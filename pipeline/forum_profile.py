#!/usr/bin/env python3
"""forum_profile.py -- ONE data record per forum, derived from the forum's own enacted text in corpus.

EPIC E-any-forum-no-hands F1 + F2 (2026-10-21). Before this module a forum lived in six code tables (md_to_docx
COURT_RULES, m1 jurisdiction block, finisher signature reservation, definitions_tagger sovereign, gate foreign-forum
patterns, gate bucket map). The nj_doi complaint needed code in all six; the next state would have needed it again.
Now every organ asks this resolver, and a new forum is a `derive --apply` run, not a code change.

Doctrine (user-locked): LLM = COMPREHENSION (which provision grants jurisdiction / lays venue / enacts U.C.C. 1-308,
which sentence is operative); deterministic = GUARDRAIL (the quote must be verbatim in the picked chunk; the cite is
read from the chunk's own `citations` column and the pinpoint from the headings that precede the quote -- the model
never writes a cite). A field nothing anchors stays EMPTY and is reported; consumers then emit a marker the filing
gate FAILs. Never another forum's text.

Data: output/audit/forum_profiles.json
  sovereigns.<SOV>: name, families{constitution,statutes,court_rules,...: [source prefixes]}, adapters{family: cmd},
                    cite_markers[] (derived from the families' citations column), fields{reservation_1308}
  courts.<COURT_ID>: sovereign, aliases[], render{court_full, caption_right[], party_header_above_caption, ...},
                    fields{jurisdiction_grant, venue_public_entity, venue_general, caption_rule}

CLI
  python3 -m pipeline.forum_profile coverage --sovereign NJ [--pull]          # F1: families present? pull missing
  python3 -m pipeline.forum_profile derive --sovereign NJ [--court NJ_SUPER_LAW --court-name "..."] [--apply]
  python3 -m pipeline.forum_profile show NJ_SUPER_LAW
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse, json, os, re, shutil, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path

BASE = Path(os.environ.get("BEEZLE_BASE", str(_eroot())))
DATA = Path(os.environ.get("BEEZLE_FORUM_PROFILES", BASE / "output" / "audit" / "forum_profiles.json"))
CHUNK_DB = Path(os.environ.get("BEEZLE_CHUNK_DB", str(_eroot() / "corpus/chunk_text.db")))
MCP = os.environ.get("BEEZLE_MCP_URL", "http://localhost:8200/mcp")

_CACHE = {"mtime": None, "data": None}


# ------------------------------------------------------------------------------------------------ resolver
def load() -> dict:
    try:
        m = DATA.stat().st_mtime
    except OSError:
        return {"sovereigns": {}, "courts": {}}
    if _CACHE["mtime"] != m:
        _CACHE["data"] = json.loads(DATA.read_text(encoding="utf-8"))
        _CACHE["mtime"] = m
    return _CACHE["data"]


def _key(court_id) -> str:
    return str(court_id or "").strip().upper().replace("-", "_").replace(" ", "_")


def court(court_id) -> dict | None:
    """The court record (aliases folded) or None. Never a default forum."""
    k = _key(court_id)
    d = load().get("courts", {})
    if k in d:
        return {"id": k, **d[k]}
    for cid, rec in d.items():
        if k in [_key(a) for a in rec.get("aliases", [])]:
            return {"id": cid, **rec}
    return None


def sovereign_of(court_id) -> str | None:
    rec = court(court_id)
    if rec:
        return rec.get("sovereign")
    k = _key(court_id)
    # legacy Pennsylvania court ids predate the profile table; they are PA by their own prefix
    if k.startswith("PA_") or "PHILA" in k:
        return "PA"
    return None


def sovereign(sov) -> dict:
    return load().get("sovereigns", {}).get(str(sov or "").upper(), {}) if sov else {}


def sovereign_name(sov, default: str = "") -> str:
    return sovereign(sov).get("name") or default


def field(court_id, name) -> dict:
    """A derived field for the court, falling back to its sovereign's record. {} when not derived."""
    rec = court(court_id) or {}
    f = (rec.get("fields") or {}).get(name)
    if f:
        return f
    return (sovereign(sovereign_of(court_id)).get("fields") or {}).get(name) or {}


def render(court_id) -> dict | None:
    rec = court(court_id)
    return dict(rec["render"]) if rec and rec.get("render") else None


def foreign_markers(sov) -> str | None:
    """Regex alternation of every OTHER sovereign's citation markers (None when none are known)."""
    pats = []
    for s, rec in load().get("sovereigns", {}).items():
        if s != str(sov or "").upper():
            pats += [re.escape(m) for m in rec.get("cite_markers", [])]
            if rec.get("name"):
                pats.append(re.escape("Commonwealth of " + rec["name"]) if s == "PA" else re.escape("State of " + rec["name"]))
    return "|".join(pats) or None


# ------------------------------------------------------------------------------------------------ corpus
def _db():
    return sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=180)


def _family_prefixes(sov: str, families=None) -> list:
    fam = sovereign(sov).get("families", {})
    out = []
    for k, v in fam.items():
        if families is None or k in families:
            out += v if isinstance(v, list) else [v]
    return out


def _prefix_count(con, prefix: str) -> int:
    r = con.execute("SELECT COUNT(*) FROM (SELECT 1 FROM chunk_text INDEXED BY ix_ct_source "
                    "WHERE source >= ? AND source < ? LIMIT 5000)", (prefix, prefix + "~")).fetchone()
    return int(r[0] or 0)


def _fts(con, phrase: str, prefixes: list, cap: int = 6) -> list:
    out = []
    try:
        # family filter IN SQL, per prefix: a LIMIT before the filter dropped the forum's rows whenever a phrase is
        # common in case law ("regularly conducts business": 196 hits, Pa.R.C.P. 2179 not among the first rows)
        q = phrase if '"' in phrase else f'"{phrase}"'
        rows = []
        for pfx in prefixes:
            rows += con.execute("SELECT c.chunk_id, c.source, c.citations, c.text FROM chunk_text_fts f "
                                "JOIN chunk_text c ON c.chunk_id = f.source_id WHERE chunk_text_fts MATCH ? "
                                "AND c.source >= ? AND c.source < ? LIMIT 50", (q, pfx, pfx + "~")).fetchall()
    except sqlite3.Error as e:
        print(f"forum_profile: FTS {phrase!r} FAILED: {e}", file=sys.stderr)
        return out
    for cid, src, cit, txt in rows:
        if any((src or "").startswith(p) for p in prefixes):
            out.append({"chunk_id": cid, "source": src, "citations": cit or "", "text": txt or "", "via": f"fts:{phrase}"})
            if len(out) >= cap:
                break
    return out


def _semantic(query: str, n: int = 10) -> list:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_chunks", "arguments": {"query": query, "n_results": n}}})
    req = urllib.request.Request(MCP, data=body.encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        raw = urllib.request.urlopen(req, timeout=120).read().decode()
        for line in raw.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                txt = d.get("result", {}).get("content", [{}])[0].get("text", "{}")
                return json.loads(txt).get("result", [])
    except Exception as e:
        print(f"forum_profile: semantic lane down ({type(e).__name__}) -- FTS lane only", file=sys.stderr)
    return []


def _chunk_by_id(con, cid: str):
    r = con.execute("SELECT chunk_id, source, citations, text FROM chunk_text WHERE chunk_id = ?", (cid,)).fetchone()
    return {"chunk_id": r[0], "source": r[1], "citations": r[2] or "", "text": r[3] or ""} if r else None


def _chunk_from_hit(con, h: dict):
    """Semantic hits carry '<source>#<vector-hash>' ids that are NOT chunk_text ids (a direct lookup silently dropped
    every semantic candidate). Resolve by the hit's own source + its text."""
    c = _chunk_by_id(con, h.get("chunk_id"))
    if c:
        return c
    src, probe = h.get("source") or "", _norm((h.get("text") or "")[:120])[:80]
    if not src or not probe:
        return None
    for cid, cit, txt in con.execute("SELECT chunk_id, citations, text FROM chunk_text INDEXED BY ix_ct_source "
                                     "WHERE source = ? LIMIT 20000", (src,)):
        if probe in _norm(txt):
            return {"chunk_id": cid, "source": src, "citations": cit or "", "text": txt or ""}
    return None


def _source_text(con, source: str, limit_chars: int = 600000) -> str:
    parts, n = [], 0
    for (t,) in con.execute("SELECT text FROM chunk_text INDEXED BY ix_ct_source WHERE source = ?",
                            (source,)):
        parts.append(t or ""); n += len(t or "")
        if n > limit_chars:
            return ""
    return "\n".join(parts)


# ------------------------------------------------------------------------------------------------ pinpoint
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman(s: str):
    s = s.upper()
    if not s or any(ch not in _ROMAN for ch in s):
        return None
    tot = 0
    for i, ch in enumerate(s):
        v = _ROMAN[ch]
        tot += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return tot


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def pinpoint(base_cite: str, full_text: str, quote: str) -> str:
    """Cite = the chunk's own citation + the locator printed ABOVE the quote in the source (Article/Section/paragraph
    or §). Read from the text, never written by the model. Returns base_cite unchanged when no locator is found."""
    base = re.sub(r"\s*\(\d{4}\)\s*$", "", (base_cite or "").strip())
    i = _ws(full_text).find(_ws(quote)[:80])
    if i < 0:
        return base
    pre = _ws(full_text)[:i]
    if re.search(r"Const\.?$", base):
        art = re.findall(r"\bARTICLE\s+([IVXLC]+)\b", pre)
        sec = re.findall(r"\bSECTION\s+([IVXLC]+|\d+)\b", pre)
        if not art and sec:
            # the source's own annotation names the article of this section ("Article VI, Section III, paragraph 1
            # amended ..."); read it from the text, never infer it
            art = re.findall(rf"\bArticle\s+([IVXLC]+),\s+Section\s+{re.escape(sec[-1])}\b", _ws(full_text), re.I)
        par = re.findall(r"(?:^|\s)(\d{1,2})\.\s+(?=[A-Z\"])", pre[-1500:] + " " + _ws(quote)[:5])
        loc = []
        if art:
            loc.append(f"art. {art[-1].upper()}")
        if sec:
            sv = _roman(sec[-1]) if not sec[-1].isdigit() else int(sec[-1])
            loc.append(f"§ {sv}")
        if par and art and sec:
            loc.append(f"¶ {par[-1]}")
        return f"{base} {', '.join(loc)}".strip() if loc else base
    # chapter-level / title-only cites ('13 Pa.C.S. ch. 13 §§ ...', '42 Pa.C.S.') take the section from the text
    chapter_level = (bool(re.search(r"§§|\bch\.", base)) or not re.search(r"\d", base)
                     or bool(re.fullmatch(r"\d+\s+[A-Za-z.\s]+", base)))
    if not chapter_level:            # per-section source: its citation is already the pinpoint
        return base
    sec = re.findall(r"§\s*(\d+[\w.\-]*)", pre[-6000:])
    title = re.split(r"\s+(?:ch\.|§)", base)[0].strip()
    return f"{title} § {sec[-1].rstrip('.')}" if (sec and title) else ""


# ------------------------------------------------------------------------------------------------ derivation
# Retrieval bounds only (candidate gathering); which provision governs is the model's call, guarded below.
FIELDS = {
    "jurisdiction_grant": {"level": "court", "families": ["constitution", "statutes"],
        "task": "the enacted provision that grants {court_name} original (subject-matter) jurisdiction over civil "
                "actions such as a tort claim against a State agency and its employees",
        "semantic": ["{sov_name} {court_name} original general jurisdiction in all causes",
                     "{sov_name} constitution judicial article trial court original jurisdiction civil actions"],
        "fts": ["original general jurisdiction", "unlimited original jurisdiction",
                "original jurisdiction of all actions", "general jurisdiction"]},
    "venue_public_entity": {"level": "court", "families": ["court_rules", "statutes"],
        "task": "the rule laying VENUE in {court_name} for actions against the State, public agencies or public "
                "officials",
        "semantic": ["{sov_name} venue actions against public agencies or officials county in which the cause of "
                     "action arose"],
        "fts": ["public agencies or officials", "against a public agency", "against the State"]},
    "venue_corporation": {"level": "court", "families": ["court_rules", "statutes"],
        "task": "the rule laying VENUE in {court_name} for an action against a corporation or similar business entity",
        "semantic": ["{sov_name} venue action against a corporation or similar entity may be brought in a county where "
                     "it regularly conducts business"],
        "fts": ["regularly conducts business", "against a corporation or similar entity",
                '"may be brought in and only in" AND "corporation"']},
    "venue_general": {"level": "court", "families": ["court_rules", "statutes"],
        "task": "the general rule laying VENUE for civil actions in {court_name} (county where the cause of action "
                "arose / where a party resides)",
        "semantic": ["{sov_name} venue in actions county in which the cause of action arose or any party resides"],
        "fts": ["county in which the cause of action arose", "venue shall be laid", "venue in all other actions",
                '"may be brought in and only in" AND "individual"']},
    "service_original_process": {"level": "court", "families": ["court_rules", "statutes"],
        "task": "the rule prescribing how the summons and complaint (original process) are served on the State, a "
                "State agency or public officer sued in {court_name}",
        "semantic": ["{sov_name} service of summons and complaint upon the State agency officer attorney general "
                     "registered certified mail",
                     "{sov_name} court rules manner of service of summons personal service upon the State by mail "
                     "to the Attorney General"],
        "fts": ['"Upon the State" AND "summons and complaint"', '"Attorney General" AND "summons and complaint"',
                "service of original process", "Service of original process upon",
                "by registered or certified mail"]},
    "service_corporation": {"level": "court", "families": ["court_rules", "statutes"],
        "task": "the rule prescribing how the summons and complaint (original process) are served on a CORPORATION or "
                "similar business entity sued in {court_name}",
        "semantic": ["{sov_name} service of original process upon a corporation or similar entity handing a copy to an "
                     "executive officer partner registered agent"],
        "fts": ["Service of original process upon a corporation", "Upon a domestic or foreign corporation",
                "Corporations and Similar Entities"]},
    "caption_rule": {"level": "court", "families": ["court_rules"],
        "task": "the court rule prescribing what must appear on the first page / caption of papers filed in "
                "{court_name} (party name, address, telephone, court, docket number)",
        "semantic": ["{sov_name} court rules caption of papers filed name of party residence address telephone "
                     "number docket number"],
        "fts": ["Above the caption", "the caption shall", "caption of the action", "shall contain a caption",
                "name of the court, the number of the action"]},
    "reservation_1308": {"level": "sovereign", "families": ["statutes"],
        "task": "{sov_name}'s enactment of U.C.C. 1-308 (performance or acceptance under reservation of rights)",
        "semantic": ["{sov_name} performance or acceptance under reservation of rights explicit reservation"],
        "fts": ["Performance or acceptance under reservation of rights", "explicit reservation of rights"]},
}

_PICK_PROMPT = """You are reading ENACTED LAW of {sov_name}. Task: identify {task}.

Below are numbered candidate excerpts from the corpus. Pick the ONE excerpt that contains that provision, and copy the
single operative sentence VERBATIM (exact words, no paraphrase, no ellipsis). If no excerpt contains it, pick null.
Excerpts of a DIFFERENT State's law are never the answer.

{cands}

Answer with JSON only: {{"pick": <number or null>, "quote": "<verbatim sentence>", "why": "<one short line>"}}"""


_SCOPE_PROMPT = """Task: identify {task}.

Candidate provision ({cite}):
"{quote}"

Is this provision the GENERAL rule for that task, or is it limited to a NARROWER class of actions or matters than the
task asks for (for example: domestic relations, medical professional liability, a single agency or statute's own
proceedings, criminal matters, appeals)? A provision that applies to the task's class of actions generally fits.

Answer with JSON only: {{"fits": true or false, "why": "<one short line>"}}"""


def _llm(prompt: str, max_tokens: int = 700) -> str:
    sys.path.insert(0, str(BASE))
    from pipeline.case_facts_refresh import _call_direct   # the proven m0 lane; never a second endpoint
    return _call_direct(prompt, max_tokens=max_tokens, timeout=300) or ""


def _json(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def _window(text: str, needle: str, before=500, after=900) -> str:
    t = _ws(text)
    i = t.lower().find((needle or "").lower()[:60]) if needle else -1
    return t[max(0, i - before): i + after] if i >= 0 else t[:before + after]


def derive_field(con, name: str, sov: str, court_name: str = "", llm=None) -> dict:
    spec = FIELDS[name]
    sov_name = sovereign_name(sov, sov)
    prefixes = _family_prefixes(sov, spec["families"])
    if not prefixes:
        return {"status": "NO_FAMILIES", "searched": spec["families"]}
    fmt = {"sov_name": sov_name, "court_name": court_name or f"the {sov_name} trial court"}
    cands, seen = [], set()
    for q in spec["semantic"]:
        for h in _semantic(q.format(**fmt), 12):
            src = h.get("source") or ""
            if any(src.startswith(p) for p in prefixes):
                c = _chunk_from_hit(con, h)
                if c and c["chunk_id"] not in seen:
                    seen.add(c["chunk_id"]); c["via"] = "semantic"; c["needle"] = (h.get("text") or "")[:60]
                    cands.append(c)
    for ph in spec["fts"]:
        for c in _fts(con, ph, prefixes, cap=4):
            if c["chunk_id"] not in seen:
                seen.add(c["chunk_id"]); c["needle"] = ph; cands.append(c)
    cands = cands[:10]
    if not cands:
        return {"status": "NO_CANDIDATES", "searched": {"prefixes": prefixes, "fts": spec["fts"]}}
    def _judge(cands):
        block = "\n\n".join(f"[{i}] source={c['source']} cite={c['citations']}\n{_window(c['text'], c.get('needle'))}"
                            for i, c in enumerate(cands))
        ans = _json((llm or _llm)(_PICK_PROMPT.format(task=spec["task"].format(**fmt), cands=block, sov_name=sov_name)))
        pick, quote = ans.get("pick"), _ws(ans.get("quote") or "")
        if pick is None or not isinstance(pick, int) or not (0 <= pick < len(cands)):
            return {"status": "NO_PICK", "why": ans.get("why", ""), "searched": [c["source"] for c in cands]}
        c = cands[pick]
        # GUARDRAIL: the quote must be the picked chunk's own words (normalized), and a real sentence
        if len(quote.split()) < 6 or _norm(quote) not in _norm(c["text"]):
            return {"status": "REJECTED_NOT_VERBATIM", "quote": quote[:200], "source": c["source"]}
        # the cite is a FACT of the source row: prefer a candidate carrying the same words AND a citation
        for alt in [c] + [x for x in cands if x is not c]:
            if _norm(quote) in _norm(alt["text"]) and (alt["citations"] or "").strip():
                c = alt
                break
        # pinpoint is read from the PICKED CHUNK only: chunk_text is WITHOUT ROWID with hash ids, so a whole-source
        # concatenation is in hash order and a heading "above" the quote there is not the quote's heading.
        full = c["text"]
        cite = pinpoint(_ws(re.sub(r'[\[\]"]', " ", c["citations"])), full, quote) if (c["citations"] or "").strip() \
            else _cite_from_form(sov, c["source"], full, quote)
        if cite.strip() and not re.search(r"§|\d:\d|\bart\.|\bR\.\s*\d|\bRule\b|Pa\.R\.C\.P\.\s*\d|\d+-\d+", cite):
            return {"status": "NO_PINPOINT", "cite": cite, "quote": quote, "source": c["source"], "chunk_id": c["chunk_id"],
                    "why": "the quote's section heading is not in its chunk and the source has no stored order; "
                           "re-ingest this source per section (F1) -- a section is never guessed"}
        if not cite.strip() or not re.search(r"[A-Za-z]", cite):
            return {"status": "NO_CITE", "quote": quote, "source": c["source"],
                    "why": "source row carries no citation; a cite is never composed"}
        # SCOPE CHECK (comprehension, second read): a verbatim quote can still be the WRONG provision -- PA caption
        # derived Pa.R.C.P. 1930.1 (domestic relations) and PA venue a medical-professional-liability clause.
        sc = _json((llm or _llm)(_SCOPE_PROMPT.format(task=spec["task"].format(**fmt), cite=cite, quote=quote), 200))
        if sc.get("fits") is not True:
            return {"status": "REJECTED_SCOPE", "cite": cite, "quote": quote, "source": c["source"],
                    "why": sc.get("why", "scope check gave no verdict")}
        return {"status": "OK", "cite": cite, "quote": quote, "source": c["source"],
                "chunk_id": c["chunk_id"], "via": c.get("via"), "derived": time.strftime("%Y-%m-%d")}

    RETRY = ("NO_CITE", "REJECTED_SCOPE", "REJECTED_NOT_VERBATIM", "NO_PINPOINT")
    r = _judge(cands)
    if r.get("status") in RETRY and len(cands) > 1:
        # drop every candidate from the rejected source+text and re-ask ONCE; the first verdict is kept on record
        bad = _norm(r.get("quote", ""))[:80]
        rest = [x for x in cands if not (x["source"] == r.get("source") and (not bad or bad in _norm(x["text"])))]
        if rest and len(rest) < len(cands):
            r2 = _judge(rest)
            r2["first_pick"] = {k: r.get(k) for k in ("status", "cite", "source", "why")}
            if r2.get("status") == "OK" or r2.get("status") in RETRY:
                return r2
    return r


def _cite_from_form(sov: str, source: str, full_text: str, quote: str) -> str:
    """Cite for a citation-less source row: the profile's registered form for the source prefix
    (sovereigns.<SOV>.cite_forms, e.g. {"pa_code_t231_": "Pa.R.C.P. {rule}"}) filled with the rule number READ from the
    last 'Rule N. Title' heading above the quote. No form or no heading -> ''."""
    forms = sovereign(sov).get("cite_forms") or {}
    form = next((v for k, v in forms.items() if (source or "").startswith(k)), None)
    if not form:
        return ""
    t = _ws(full_text)
    i = t.find(_ws(quote)[:80])
    if i < 0:
        return ""
    heads = re.findall(r"\bRule\s+(\d{1,4}(?:\.\d+)?)\.\s+[A-Z]", t[max(0, i - 20000):i])
    return form.format(rule=heads[-1]) if heads else ""


def cite_markers(con, sov: str, top: int = 6) -> list:
    """Citation markers of a sovereign, READ from its families' citations column ('N.J.S.A. 59:2-2' -> 'N.J.S.A.')."""
    from collections import Counter
    cnt = Counter()
    for p in _family_prefixes(sov):
        for (cit,) in con.execute("SELECT citations FROM chunk_text INDEXED BY ix_ct_source WHERE source >= ? AND "
                                  "source < ? LIMIT 3000", (p, p + "~")):
            m = re.sub(r"\([^)]*\)|[\[\]\"]", " ", cit or "")
            m = _ws(re.sub(r"(?:§+|\bch\.|\bart\.|[\d][\w:.\-()]*)", " ", m)).strip(" ,;")
            if m and len(m) <= 24 and re.search(r"[A-Za-z]\.", m):
                cnt[m] += 1
    return [m for m, n in cnt.most_common(top) if n >= 3]


def coverage(sov: str, pull: bool = False) -> dict:
    """F1: which of the sovereign's law families are in corpus; run the registered adapter for a missing one;
    no adapter -> the gap is reported (and carded by the caller), never silently passed."""
    rec = sovereign(sov)
    out = {}
    con = _db()
    try:
        for fam, prefixes in (rec.get("families") or {}).items():
            n = sum(_prefix_count(con, p) for p in (prefixes if isinstance(prefixes, list) else [prefixes]))
            out[fam] = {"chunks": n, "status": "PRESENT" if n else "MISSING"}
    finally:
        con.close()
    for fam in ("constitution", "statutes", "court_rules"):
        out.setdefault(fam, {"chunks": 0, "status": "MISSING", "note": "no source family registered"})
    if pull:
        for fam, st in out.items():
            if st["status"] != "MISSING":
                continue
            cmd = (rec.get("adapters") or {}).get(fam)
            if not cmd:
                st["action"] = "NO_ADAPTER: register a puller for this family (legal-corpus-ingest)"
                continue
            r = subprocess.run(cmd, shell=True, cwd=str(BASE), capture_output=True, text=True, timeout=7200)
            st["action"] = f"pulled rc={r.returncode}"
    return out


# ------------------------------------------------------------------------------------------------ write
def _save(d: dict):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    if DATA.exists():
        bak = BASE / ".backups" / f"forum_profiles.json.{time.strftime('%Y%m%d_%H%M%S')}.bak"
        bak.parent.mkdir(exist_ok=True); shutil.copy2(DATA, bak)
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA)


def derive(sov: str, court_id: str = "", court_name: str = "", apply: bool = False) -> dict:
    d = json.loads(json.dumps(load()))
    srec = d.setdefault("sovereigns", {}).setdefault(sov, {})
    if not srec.get("families"):
        raise SystemExit(f"forum_profile: sovereign {sov} has no source families registered -- run coverage first")
    con = _db()
    try:
        srec["cite_markers"] = cite_markers(con, sov) or srec.get("cite_markers", [])
        report = {"sovereign": sov, "cite_markers": srec["cite_markers"]}
        for name, spec in FIELDS.items():
            if spec["level"] == "sovereign":
                r = derive_field(con, name, sov)
                report[name] = r
                if r["status"] == "OK":
                    srec.setdefault("fields", {})[name] = r
        if court_id:
            crec = d.setdefault("courts", {}).setdefault(_key(court_id), {"sovereign": sov})
            crec["sovereign"] = sov
            if court_name:
                crec["name"] = court_name
            for name, spec in FIELDS.items():
                if spec["level"] == "court":
                    r = derive_field(con, name, sov, crec.get("name") or court_name)
                    report[name] = r
                    if r["status"] == "OK":
                        crec.setdefault("fields", {})[name] = r
    finally:
        con.close()
    if apply:
        _save(d)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forum_profile")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c1 = sp.add_parser("coverage"); c1.add_argument("--sovereign", required=True); c1.add_argument("--pull", action="store_true")
    c2 = sp.add_parser("derive"); c2.add_argument("--sovereign", required=True); c2.add_argument("--court", default="")
    c2.add_argument("--court-name", default=""); c2.add_argument("--apply", action="store_true")
    c3 = sp.add_parser("show"); c3.add_argument("court")
    a = ap.parse_args(argv)
    if a.cmd == "coverage":
        print(json.dumps(coverage(a.sovereign.upper(), a.pull), indent=1))
    elif a.cmd == "derive":
        print(json.dumps(derive(a.sovereign.upper(), a.court, a.court_name, a.apply), indent=1, ensure_ascii=False))
    else:
        print(json.dumps({"court": court(a.court), "sovereign": sovereign(sovereign_of(a.court))}, indent=1,
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
