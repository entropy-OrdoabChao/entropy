#!/usr/bin/env python3
"""cells_to_stub.py — DETERMINISTIC organ: merged m2 + room cells  ->  the m0-shaped stub m1's
APPLICATION lane reads (pipeline.m1 parse_stub + pipeline.m1_application).

WHY DETERMINISTIC (user doctrine 2026-10-24): the reasoning already happened in m2 (per-unit bots) and
in the room (lane panel). This organ only TRANSCRIBES grounded rows into the stub anatomy; it never
composes a sentence that is not in a row. Deterministic = guardrail: what is not in a GROUNDED,
corpus-pinned row cannot reach the filing. m1's own refusals (zero RELIEF SHAPE items, a missing
`PLEAD, verbatim:` line) stay in force downstream — nothing here fills a template to get past them.

INPUT ROWS (jsonl, append-only; NEWEST row per (origin, bot, claim) wins):
  m2 cells   bot, claim(int unit no.), title, status, level, finding, proposition,
             authorities[{cite, quote, source, verified, verify, chunk_id?}], gap, fix
  room cells bot=<lane>, claim, status, finding, proposition ('Rule: "..." <cite> [corpus:<chunk>].
             Violation: ...'), authorities, record_facts, relay
  posture    (sibling task; may be absent) bot='posture' | lane in {posture-vehicle, dismissal-posture}
             with claim=0, or any row carrying a `vehicle` key:
             {vehicle, rule_verbatim, trigger_event, trigger_date, deadline_rule, deadline_date,
              standard, must_contain, authorities}
  remedy     bot startswith 'remedy': {relief_item, rule_verbatim, record_fact, authorities} or the
             older {finding, proposition}.

PIN RESOLUTION (a row is usable only when >= 1 authority resolves to a corpus chunk/doc id):
  authority.chunk_id  ->  'chunk=<id>' inside authority.source  ->  authority.source when it is a
  corpus id (32-hex / *_chunk_N / doc slug, never a 'cite:' or a citation string)  ->  authority.verify
  when verified is true and verify is a corpus id  ->  '[corpus:<id>]' tags inside the row's own
  proposition (room rows pin inline). Anything else = UNPINNED and is listed, never pleaded.

OUTPUT STUB SECTIONS (m1.parse_stub ignores unknown '## ' sections, so VEHICLE / GAPS are safe):
  # DRAFT - <title> + **meta** lines
  ## VEHICLE                      posture row rendered, or 'VEHICLE: NOT DERIVED (no posture cell)'
  ## CONTROLLING AUTHORITY        ### <unit title> / - PLEAD, verbatim: <proposition> <cite> [corpus:id]
  ## RELIEF SHAPE                 numbered items from remedy rows, else posture must_contain/vehicle
  ## Administrative history (PROCEDURAL HISTORY)   bullets from CASE_TIMELINE (parse_stub key)
  ## NEW EVIDENCE BLOCK (EVIDENCE)                 ### <L> — <record document title> / Element / Date / Source [doc:<path>]
                                                   (manifest <out dir>/EXHIBITS.md verbatim, else derived from the record docs
                                                   the cells/units/timeline cite — NEVER a unit's prose; --exhibits overrides)
  ## GAPS (not pleaded)           every non-GROUNDED or unpinned (unit, bot) with its gap text

CLI
  python3 cells_to_stub.py --cells <a.jsonl> [<b.jsonl> ...] --units ADVERSE_POINTS.md --case <slug>
      --court PA_CP_PHILA --type motion_to_vacate --out <stub.md> [--timeline CASE_TIMELINE.json]
      [--write-cf [--case-facts <CASE_FACTS.md>]]
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse
import collections
import datetime as _dt
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

# ROOT = the live repo (sandbox copies live under <ROOT>/_sandbox_*/pipeline/m2/); the CASE_FACTS writer is
# IMPORTED from the live organ runner, never re-implemented (one writer, one block format).
ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "pipeline" / "m1.py").exists():
    ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = ROOT / "obsidian_sync" / "cases"

# S-cite-drift-normalize (2026-10-24, run 42): every CASE authority is pinned to the corpus source key at LOAD time —
# a drifted reporter cite (Werner 809/808 A.2d vs the pin 799 A.2d 776) is rewritten to the pin and logged; a name that
# resolves to no pinned source is status FETCH and never pleaded. The organ is the SIBLING cite_normalize.py (sandbox
# copies carry their own); CITE_NORMALIZE=off disables (reported loudly — a gate that quietly no-ops is the defect).
_CN = None
_RESOLVER = None
NORMALIZE_LOG: list = []


def _sibling_organ(name: str, mod_name: str):
    """Load a sibling organ from the file NEXT TO THIS ONE (a sandbox copy gets its own), else the live repo's.
    `pipeline` is a regular package: a sandbox dir on sys.path shadows the WHOLE live package and silently
    un-pins every cell, so sibling organs are loaded by path. Same pattern as _cite_normalizer below."""
    p = Path(__file__).resolve().parent / name
    if not p.exists():
        p = ROOT / "pipeline" / "m2" / name
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cite_normalizer():
    """-> (module, Resolver) or (None, None) when disabled / unavailable (printed once)."""
    global _CN, _RESOLVER
    if _RESOLVER is not None or _CN is False:
        return (_CN or None), _RESOLVER
    if os.environ.get("CITE_NORMALIZE", "").lower() in ("off", "0", "none"):
        print("  [cite-normalize] DISABLED via CITE_NORMALIZE=off -- cells NOT pinned to the corpus")
        _CN = False
        return None, None
    try:
        p = Path(__file__).resolve().parent / "cite_normalize.py"
        if not p.exists():
            p = ROOT / "pipeline" / "m2" / "cite_normalize.py"
        spec = importlib.util.spec_from_file_location("cite_normalize", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RESOLVER = mod.Resolver()
        _RESOLVER.keys()                       # fail here (not mid-row) when the corpus db is unreachable
        _CN = mod
    except Exception as e:
        print(f"  [cite-normalize] WARNING: unavailable ({e!r}) -- cells NOT pinned to the corpus")
        _CN = False
        return None, None
    return _CN, _RESOLVER


def normalize_rows(rows: list) -> list:
    """cite_normalize.normalize_row over every row; -> the normalized/fetch results (also kept in NORMALIZE_LOG)."""
    cn, R = _cite_normalizer()
    if not cn:
        return []
    out = []
    for r in rows:
        for res in cn.normalize_row(r, R):
            if res["action"] in ("normalized", "fetch"):
                res = dict(res, bot=r.get("bot"), claim=r.get("claim"), origin=r.get("_origin"))
                out.append(res)
    NORMALIZE_LOG.extend(out)
    return out

TITLE_OF = {
    "motion_to_vacate": "Motion to Vacate",
    "motion_for_reconsideration": "Motion for Reconsideration",
    "petition_341c": "Petition for Determination under Pa.R.A.P. 341(c)",
}
POSTURE_BOTS = {"posture", "posture-vehicle", "dismissal-posture"}
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_CORPUS_TAG = re.compile(r"\[\s*corpus\s*:\s*([^\]\s]+)\s*\]", re.I)
_CORPUS_TAG_OPEN = re.compile(r"\[\s*corpus\s*:\s*([^\]\s]+)", re.I)   # a cite whose tag lost its ']' still names the chunk
_CHUNK_EQ = re.compile(r"chunk=([^\s\]]+)")
_RELIEF_VERB = re.compile(
    r"\b(vacat\w*|rescind\w*|modif\w*|str(?:ike|icken)|reinstat\w*|leave to (?:amend|plead)|plead over|"
    r"reconsider\w*|set aside|afford\w*|open(?:ed)? the|amend)\b", re.I)
_RELIEF_MODAL = re.compile(r"\b(must|shall|should|is entitled to|are entitled to|is required to|may not|cannot)\b", re.I)


def _is_relief_clause(s: str) -> bool:
    """The older remedy shape has no relief_item; a sentence counts as its relief clause only when it is DIRECTIVE
    (modal / entitlement) AND names a relief act — never a bare statement of harm ('the right to amend terminates')."""
    return bool(_RELIEF_VERB.search(s) and _RELIEF_MODAL.search(s))


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")

# ------------------------------------------------------------------ S-stance-input (run 53): the user's amend-vs-stand DECISION is an input
# Runs 48-52 let the posture-vehicle / remedy lanes CHOOSE 'Motion to Vacate and for Leave to Amend' (Rule 1033(a) vehicle, Relief 3 =
# leave to file a First Amended Complaint, Exhibits K/L). The decision is the user's, not a lane's: --stance stand (env STANCE) drops,
# deterministically, every row/item/exhibit whose relief or vehicle is amendment (regex below), selects the § 5505 posture row as the
# VEHICLE, and transcribes the stance's relief items — each ONLY when a GROUNDED, corpus-pinned row carries the cite it rests on
# (§ 5505 → vacate + reinstate/answer; Rule 2.11(A) → disqualification/reassignment, i.e. the impartiality unit sustained).
# --stance amend (default) = byte-identical to before. Everything dropped is listed in the stub's GAPS as 'STANCE=stand: dropped …'.
_AMEND_RX = re.compile(r"leave to (?:amend|plead(?: over)?)|leave to file (?:a |an |the )?(?:\w+ )?amended|amended complaint|\b1033\b|plead over|"
                       r"amend(?:ing|ment of)? (?:the|its|his|her|plaintiff'?s?) (?:complaint|pleading)|comparison copy|clean copy|first amended", re.I)
_RULE211_RX = re.compile(r"2\.11|impartiality might reasonably be questioned|disqualif", re.I)
STANCE_DROPPED: list = []


def _row_text(r: dict) -> str:
    return " ".join(str(r.get(k) or "") for k in ("vehicle", "relief_item", "proposition", "finding", "rule_verbatim", "record_fact", "title")) + " " + \
        " ".join(str(a.get("cite") or "") for a in (r.get("authorities") or []) if isinstance(a, dict))


def _cites_5505(r: dict) -> bool:
    return "5505" in _row_text(r)


# S-chain-ftype: the STAND vehicle is per FILING TYPE (the user's decision), not a hard-wired § 5505. Each entry = the cite that
# marks the vehicle's posture row + the relief items transcribed ONLY where a GROUNDED pinned row carries the cite (same rule as
# before). motion_to_vacate = byte-identical to the pre-S-chain-ftype behaviour.
FILING_TYPES_DIR = (_eroot() / "obsidian_sync/templates/filing_types")
_VEHICLE_CACHE: dict = {}


def vehicle_of(ftype: str) -> dict:
    """The STAND vehicle is DATA in the filing-type YAML (`vehicle:` block in templates/filing_types/<ftype>.yaml):
    key_cites (the cite marking the vehicle's posture row) + relief items ({text, rests_on, dedupe}). A filing type
    without a `vehicle:` block transcribes no relief (GAPS says so) — never a Python default for another forum."""
    if ftype in _VEHICLE_CACHE:
        return _VEHICLE_CACHE[ftype]
    v = {"key": (), "relief": [], "exclude": []}
    f = FILING_TYPES_DIR / f"{ftype}.yaml"
    if f.is_file():
        try:
            import yaml
            blk = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("vehicle") or {}
            v["key"] = tuple(str(k) for k in (blk.get("key_cites") or []))
            v["exclude"] = [str(x) for x in (blk.get("exclude_relief") or []) if str(x).strip()]
            v["relief"] = [(str(it.get("text") or ""), tuple(str(k) for k in (it.get("rests_on") or [])),
                            tuple(str(x) for x in (it.get("dedupe") or ["", ""]))[:2])
                           for it in (blk.get("relief") or []) if isinstance(it, dict) and it.get("text")]
        except Exception as e:                        # a malformed block is a loud GAP, not a silent fallback
            v["error"] = f"{f.name}: {e}"
    _VEHICLE_CACHE[ftype] = v
    return v


STANCE_FTYPE = "motion_to_vacate"       # set by build_stub from --type; read by stance_filter/stance_relief

# ---- S-no-hardcode (2026-11-14): the record-attribution guard below matched the literal
# "Progressive (wrote|said)" (and an adverse employee's first name), so a quotation attributed to
# ANOTHER case's adverse party was read as a record fact while this case's adverse party was not
# recognised at all. The adverse party's names now come from pipeline.case_profile for the case
# under analysis; with no case set, only the ROLE words (which name no party) are used.
CASE_SLUG = None                        # set from --case in main(); read by _adverse_alt()
_ADV_ALT_CACHE = {}


def _adverse_alt() -> str:
    """Regex alternation of the adverse party's names for CASE_SLUG, every name re.escape'd.
    No case / no data -> '' (the guard then relies on the role words alone, never on a party)."""
    key = CASE_SLUG or ""
    if key in _ADV_ALT_CACHE:
        return _ADV_ALT_CACHE[key]
    alt = ""
    if key:
        try:
            from pipeline.case_profile import profile as _cp
            names = list(_cp(key).get("adverse_names") or [])
            alt = "|".join(re.escape(n) for n in names)
        except Exception as e:                           # unregistered case -> role words only
            print(f"  [adverse-rx] no case profile for {key!r} ({e}); role words only")
    _ADV_ALT_CACHE[key] = alt
    return alt


def _vehicle() -> dict:
    return vehicle_of(STANCE_FTYPE)


def _cites_key(r: dict) -> bool:
    t = _row_text(r)
    return any(k in t for k in _vehicle()["key"])


def stance_filter(rows: list, stance: str) -> tuple:
    """-> (rows, dropped). stand: posture rows whose vehicle is amendment and remedy rows whose relief is amendment are dropped;
    the vehicle's posture row (key cite of VEHICLE_OF[ftype]; GROUNDED first) is moved to the front so build_stub's 'first posture
    row' IS the vehicle."""
    if stance != "stand":
        return rows, []
    keep, dropped = [], []
    for r in rows:
        txt = _row_text(r)
        if _is_posture(r) and _AMEND_RX.search(txt) and not _cites_key(r):
            dropped.append(("posture", r)); continue
        if _is_remedy(r) and _AMEND_RX.search(str(r.get("relief_item") or r.get("proposition") or r.get("finding") or "")):
            dropped.append(("remedy", r)); continue
        keep.append(r)
    post = [r for r in keep if _is_posture(r)]
    pref = sorted(post, key=lambda r: (not _cites_key(r), not _grounded(r)))
    rows2 = pref + [r for r in keep if not _is_posture(r)]
    STANCE_DROPPED[:] = dropped
    return rows2, dropped


def stance_relief(rows: list, items: list, units_doc: dict, counts: str) -> list:
    """stand: the stance's relief items, transcribed ONLY where a GROUNDED pinned row carries the cite each rests on; items already
    present (same act + same cite) are not duplicated. -> the added items (each '<text>. <cite> [corpus:<chunk>]')."""
    g = [r for r in rows if _grounded(r) and not _is_posture(r)]
    def _pins_for(keys):
        if "2.11" in keys:
            return [p for r in g for p in pins_of(r) if _RULE211_RX.search((p["cite"] or "") + " " + (p["quote"] or "")) or "2.11" in str(r.get("proposition") or "")[:400]][:1]
        return [p for r in g for p in pins_of(r) if any(k in (p["cite"] or "") for k in keys)][:1]
    on = re.search(r"\bORDER_(\d+)\b", units_doc.get("header") or ""); order = f"ORDER_{on.group(1)}" if on else "the order"
    cno = units_doc.get("control_no") or ""; ent = units_doc.get("entered") or ""
    label = f"{order}" + (f" (Control No. {cno}" + (f", entered {ent}" if ent else "") + ")" if cno else "")
    cl = ", ".join(c.strip() for c in (counts or "").split(",") if c.strip())
    want = [(txt.format(label=label, counts=cl), _pins_for(keys), ac) for txt, keys, ac in _vehicle()["relief"]]
    added = []
    for txt, pins, (act, cite) in want:
        if not pins:
            continue                                   # no GROUNDED pinned row carries the cite → not pleaded (GAPS says so)
        if any(act in it.lower() and (not cite or cite in it) for it in items + added):
            continue
        tail = "; ".join(f"{p['cite']} [corpus:{p['chunk']}]" if p["cite"] else f"[corpus:{p['chunk']}]" for p in pins)
        added.append(f"{txt}. {tail}")
    return added


# ------------------------------------------------------------------ small helpers
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _cite_key(c: str) -> str:
    """Both cite forms key the same: 'Gregg v. Ameriprise Fin., 245 A.3d 637 (Pa. 2021)' ~ 'Gregg v. Ameriprise'."""
    c = re.sub(r"\s*\([^)]*\)\s*$", "", (c or "").strip())
    c = re.sub(r"\s*\[corpus:[^\]]*\]", "", c)
    c = c.split(",")[0]
    return _key(c)[:40]


def _is_corpus_id(s) -> bool:
    if not isinstance(s, str) or not s or " " in s or s.lower().startswith(("cite:", "not in corpus")):
        return False
    if re.match(r"^(Pa\.|Rule\b|\d+\s|Code\b|U\.S\.|Fed\.)", s):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-#.]{5,}", s))


def pins_of(row: dict) -> list:
    """[{cite, chunk, quote}] — every authority of the row that resolves to a corpus pin (see module doc)."""
    out, seen = [], set()
    text_tags = _CORPUS_TAG.findall((row.get("proposition") or "") + " " + (row.get("rule_verbatim") or ""))
    for a in row.get("authorities") or []:
        if not isinstance(a, dict):
            continue
        if (a.get("status") or "").upper() == "FETCH":        # S-cite-drift-normalize: a name with no corpus pin is never pleaded
            continue
        chunk = None
        src = a.get("source") or ""
        # an authority the verifier marked NOT IN CORPUS is never a pin, whatever its source string says
        # (the source is where the verifier LOOKED, not where the quote WAS)
        unverified = a.get("verified") is False
        if a.get("chunk_id"):
            chunk = str(a["chunk_id"])
        elif _CHUNK_EQ.search(src):
            chunk = _CHUNK_EQ.search(src).group(1)
        elif _CORPUS_TAG_OPEN.search(a.get("cite") or ""):
            chunk = _CORPUS_TAG_OPEN.search(a.get("cite") or "").group(1)
        elif unverified:
            chunk = None
        elif _is_corpus_id(src):
            chunk = src
        elif a.get("verified") is True and _is_corpus_id(a.get("verify")):
            chunk = a["verify"]
        if chunk and unverified and str(a.get("verify") or "").upper().startswith("NOT IN CORPUS"):
            chunk = None
        if not chunk and text_tags and len(row.get("authorities") or []) == 1:
            chunk = text_tags[0]            # single-authority room row pinned inline
        if chunk and (a.get("cite"), chunk) not in seen:
            seen.add((a.get("cite"), chunk))
            out.append({"cite": re.split(r"\s*\.?\s*Must contain:", _CORPUS_TAG_OPEN.sub("", a.get("cite") or ""), 1)[0].strip(" []."), "chunk": chunk,
                        "quote": _norm(a.get("quote") or "")})
    if not out and text_tags:
        # room rows with authorities=[] pin inline: 'Rule: "..." <cite> [corpus:<id>]'
        prop = row.get("proposition") or ""
        for m in _CORPUS_TAG.finditer(prop):
            before = prop[:m.start()]
            q = max(before.rfind("”"), before.rfind('"'))
            cite = _norm(before[q + 1:]) if q >= 0 and len(before) - q <= 90 else ""
            cite = cite.strip(" ,;—-")
            if (cite, m.group(1)) not in seen:
                seen.add((cite, m.group(1)))
                out.append({"cite": cite, "chunk": m.group(1), "quote": ""})
    return out


def _is_posture(row: dict) -> bool:
    if "vehicle" in row:
        return True
    try:
        c0 = int(row.get("claim") or 0) == 0
    except Exception:
        c0 = False
    return c0 and (row.get("bot") or "").lower() in POSTURE_BOTS


def _is_remedy(row: dict) -> bool:
    return (row.get("bot") or "").lower().startswith("remedy") and not _is_posture(row)


def _grounded(row: dict) -> bool:
    return (row.get("status") or "").upper() == "GROUNDED"


# ------------------------------------------------------------------ loaders
_QUOTE_RX = re.compile(r"[\"“]([^\"”]{20,400})[\"”]")


def _verify_quote(q: str) -> dict:
    try:
        from pipeline.m2.analysis_runner import verify_quote
        return verify_quote(q)
    except Exception as e:                                   # verifier unreachable -> refuse, never fail-open
        return {"ok": False, "why": f"verifier unavailable: {e}"}


def _quote_guard(r: dict) -> None:
    """GUARDRAIL (S-quote-verbatim, 2026-10-26): every quotation a cell attributes to an authority — authorities[].quote
    and every 'Rule: "…"' / "…" span of the proposition — must exist verbatim in the corpus (FTS phrase, first 12 words).
    A row carrying ANY quotation the corpus does not hold is demoted GROUNDED -> UNVERIFIED-QUOTE with the gap named, so
    it lands in GAPS, never in the paper. Cause: the cert341c room quoted Pa.R.C.P. 1028(c)(2) as 'the court shall specify
    in its order the grounds …' — words the rule does not contain — and the cite gate (case cites only) passed it."""
    if (r.get("status") or "").upper() != "GROUNDED" or os.environ.get("M2_QUOTE_GUARD") == "off":
        return
    quotes = []
    for a in r.get("authorities") or []:
        if isinstance(a, dict) and a.get("quote"):
            quotes.append((a.get("cite") or "?", str(a["quote"])))
    prop = str(r.get("proposition") or "")
    for m in _QUOTE_RX.finditer(prop):
        tail = prop[m.end():m.end() + 160]
        # a quotation attributed to the RECORD ([doc:…], Exhibit, email, the order) is a record fact, not a corpus quote
        adv = _adverse_alt()
        said = r"(?:wrote|said|told|stated|represented|advised)"
        who = r"(?:Defendant|Plaintiff|counsel|regulator|Department" + (("|" + adv) if adv else "") + r")"
        base = r"\[doc:|\[record|Exhibit|Ex\.\s*[A-Z]|e-?mail|ORDER_\d"
        if re.search(base + r"|the Order|" + who + r"\s+" + said, tail, re.I) \
           or re.search(base + r"|" + who + r"\s+" + said, prop[max(0, m.start() - 120):m.start()], re.I):
            continue
        quotes.append(("proposition", m.group(1)))
    bad = []
    for cite, q in quotes:
        q = re.sub(r"\s+", " ", q).strip()
        if len(q.split()) < 6:
            continue
        v = _verify_quote(q)
        if not v.get("ok"):
            bad.append(f"{cite}: \"{q[:70]}…\" — {v.get('why')}")
    if bad:
        r["status"] = "UNVERIFIED-QUOTE"
        r["gap"] = "QUOTE NOT IN CORPUS (verbatim guard): " + " | ".join(bad)


def load_cells(paths) -> tuple:
    """-> (rows, counts). Newest row per (origin, bot, claim) — the files are append-only."""
    newest, counts = collections.OrderedDict(), collections.OrderedDict()
    for p in paths:
        p = Path(p)
        n = 0
        origin = "room" if "room" in p.name.lower() else "m2"
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            n += 1
            r = dict(r)
            r["_origin"] = "room" if (origin == "room" or r.get("relay")) else "m2"
            r["_file"] = str(p)
            try:
                r["claim"] = int(r.get("claim"))
            except Exception:
                r["claim"] = 0 if _is_posture(r) else r.get("claim")
            # S-room-sibling-cards (run 53): a room lane writes SEVERAL cards on one unit (relay '<tag> r<k> <lane>#<n>' is unique per
            # card); keying on (origin, bot, claim) alone kept ONE of them (r52: 137 rows → 92). The relay id joins the key; m2 rows
            # (no relay) and re-runs of the same relay id still resolve newest-wins.
            _quote_guard(r)                 # S-quote-verbatim: a quotation the corpus does not hold never reaches a court paper
            newest[(r["_origin"], (r.get("bot") or "").lower(), r["claim"], str(r.get("relay") or ""))] = r
        counts[str(p)] = n
    rows = list(newest.values())
    normalize_rows(rows)                                         # S-cite-drift-normalize: pin every case cite before anything reads the rows
    return rows, counts


def load_units(path) -> dict:
    """ADVERSE_POINTS.md -> {header, control_no, entered, docketed, units:[{n,title,body,record_facts}]}"""
    t = Path(path).read_text(encoding="utf-8", errors="replace")
    header = t.splitlines()[0] if t else ""
    units = []
    heads = list(re.finditer(r"^\*\*(\d+)\.\s+(.+?)\*\*\s*$", t, re.M))
    for i, m in enumerate(heads):
        body = t[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(t)]
        facts = []
        for ln in body.splitlines():
            s = ln.strip()
            if re.match(r"^(?:-\s*)?Record facts?\s*:", s, re.I):
                facts.append(_norm(re.sub(r"^(?:-\s*)?Record facts?\s*:\s*", "", s, flags=re.I)))
            elif re.match(r"^PLAINTIFF'S POSITION OF RECORD", s):
                facts.append(_norm(re.sub(r"^PLAINTIFF'S POSITION OF RECORD[^:]*:\s*", "", s)))
        units.append({"n": int(m.group(1)), "title": _norm(m.group(2)), "body": body, "record_facts": facts})
    cno = re.search(r"Control No\.?\s*(\d{6,})", header)
    ent = re.search(r"entered\s+(\d{4}-\d{2}-\d{2})", header)
    dock = re.search(r"docketed\s+(\d{4}-\d{2}-\d{2})", header)
    return {"header": header, "control_no": cno.group(1) if cno else "",
            "entered": ent.group(1) if ent else "", "docketed": dock.group(1) if dock else "", "units": units}


_RECORD_TYPES = ("COURT_", "ADVERSE_", "ORDER", "FILED", "FILING", "SERVICE_COMPLETE", "DOCKET", "HEARING", "JUDGMENT", "APPEAL")
_INTERNAL_RX = re.compile(r"READY FOR PAYMENT|LOADED to LetterStream|CASE_FACTS|purged|guard hardened|pipeline|cron|sandbox|\bbackfill\b|user paid|RECORD CORRECTION|INTAKE INTEGRATED|automated case_facts|EMAIL —|LetterStream Job|confirmation email received", re.I)


def _is_record_event(e: dict) -> bool:
    """A CASE_TIMELINE entry is a COURT-RECORD event (transcribable into PROCEDURAL HISTORY) when it carries a
    docket_ref or a record type, and its text is not an internal operations note. The guardrail is deterministic:
    the LLM never decides what the court may see."""
    txt = " ".join(str(e.get(k) or "") for k in ("event", "description", "summary", "detail"))
    if _INTERNAL_RX.search(txt):
        return False
    typ = str(e.get("type") or e.get("kind") or "").upper()
    if e.get("docket_ref") or e.get("ecf") or e.get("control_no"):
        return True
    return any(typ.startswith(t) or t in typ for t in _RECORD_TYPES)


_OPS_SENTENCE = re.compile(
    r"\s*(?:Source of record|Source|Fetched from|OCR(?: at)?|text at|see record/|record/orders/|LetterStream|CASE_FACTS|"
    r"ANSWER due)[^.]*\.?(?:\s*\([^)]*\))?\.?", re.I)
# '<Party> ANSWER due <date>.' is OUR deadline note, whoever the party is — so the party name is matched
# STRUCTURALLY (capitalized words at a sentence start, immediately before "ANSWER due"), never by name.
# Case-SENSITIVE on purpose: re.I would let the prefix swallow ordinary lowercase words and eat the whole
# sentence, and _court_facing_event's `out or txt` fallback would then silently return the ops note unstripped.
_PARTY_ANSWER_DUE = re.compile(r"(?:(?<=\.)|^)\s*(?:[A-Z][A-Za-z&.'\-]*\s+){1,3}(?=ANSWER due)", re.M)


def _court_facing_event(txt: str) -> str:
    """CASE_TIMELINE events carry our own provenance/ops notes ('Source of record: record/orders/X.pdf (fetched from
    fjdefile..., OCR at 300dpi)', 'ANSWER due ...'). Those are bookkeeping for us, not a fact the court reads in
    PROCEDURAL HISTORY. Strip sentence-wise; keep the record event itself. Deterministic guardrail at stub write."""
    out = _OPS_SENTENCE.sub("", _PARTY_ANSWER_DUE.sub("", txt))
    out = re.sub(r"\bORDER_\d+(?:_\d+)?\.(?:pdf|txt)\b,?\s*", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ;,")
    return out or txt


def load_timeline(path) -> list:
    if not path or not Path(path).exists():
        return []
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    # CASE_TIMELINE.json carries BOTH `timeline` (the docket) and `entries` (ops notes); until 2026-10-27 only
    # `entries` was read, so PROCEDURAL HISTORY had the LetterStream loads and lacked the complaint and the POs.
    ents = (list(d.get("timeline") or []) + list(d.get("entries") or [])) if isinstance(d, dict) else d
    out, seen = [], set()
    for e in ents or []:
        if not isinstance(e, dict):
            continue
        if not _is_record_event(e):          # S-timeline-leak: internal ops (LetterStream loads, vault corrections) never reach a court paper
            continue
        txt = e.get("event") or e.get("description") or e.get("summary") or ""
        if e.get("detail") and not e.get("event") and not e.get("description"):
            txt = f"{txt} {e['detail']}".strip()
        if e.get("date") and txt:
            k = (str(e["date"]), _norm(txt)[:60].lower())
            if k in seen:
                continue
            seen.add(k); out.append((str(e["date"]), _norm(txt)))
    return sorted(out, key=lambda x: x[0])


# ------------------------------------------------------------------ assembly
def _plead_line(text: str, pins: list) -> str:
    """'- PLEAD, verbatim: <text>' + ' <cite> [corpus:<chunk>]' for every pin not already in the text."""
    text = _norm(text).rstrip()
    tail = []
    for p in pins:
        if p["chunk"] in text:
            continue
        tail.append(f"{p['cite']} [corpus:{p['chunk']}]" if p["cite"] else f"[corpus:{p['chunk']}]")
    if tail:
        text = text.rstrip(".") + ". " + "; ".join(tail)
    return f"- PLEAD, verbatim: {text}"


def _go_backstop_heading(bs: dict) -> str:
    """The backstop's own spine heading (an authority_spine key in m1 — short and stable, the full sentence
    lives on the `- PLEAD, verbatim:` line below it)."""
    i = _norm(bs.get("interest") or "")
    return "CONSTITUTIONAL BACKSTOP (last ground" + (f"): {i[:150]}" if i else ")")


def _shingles(t: str) -> set:
    w = re.findall(r"[a-z0-9\u00a7]+", t.lower())
    return {tuple(w[i:i + 3]) for i in range(max(0, len(w) - 2))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def build_spine(rows: list, units: list, labels: dict | None = None) -> tuple:
    """-> (lines, used_rows, zero_units). Room row wins on (unit, cite); the m2 quote survives as a sub-line.

    S-avoidance-order (2026-11-14): `units` arrive ALREADY ORDERED narrow -> broad by pipeline.m2.ground_order
    and `labels[n]` carries that unit's ground number + class (a-d), emitted as a marker bullet under the
    heading. The heading TEXT is untouched (m1.parse_stub keys authority_spine by it) and the marker is not a
    `- PLEAD` line, so nothing downstream reads it as a pleading."""
    by_unit = collections.defaultdict(list)
    for r in rows:
        if _is_posture(r) or _is_remedy(r) or not _grounded(r):
            continue
        pins = pins_of(r)
        if not pins or not _norm(r.get("proposition") or ""):
            continue
        by_unit[r["claim"]].append((r, pins))
    lines, used, zero = [], [], []
    for u in units:
        lines.append(f"### {u['title']}")
        lab = (labels or {}).get(u["n"])
        if lab:
            lines.append(f"- GROUND {lab['seq']} [class {lab['class']} — {lab['class_name']}] "
                         f"(constitutional-avoidance ordering; {lab['src']})")
        cand = by_unit.get(u["n"], [])
        room = [(r, p) for r, p in cand if r["_origin"] == "room"]
        m2 = [(r, p) for r, p in cand if r["_origin"] != "room"]
        pleads = []                       # [{text, pins, cites:set, quotes:[]}]
        seen_text = set()
        for r, p in room + m2:
            txt = _norm(r["proposition"])
            # a card's checklist tail ("Must contain: (a) copy of the order ...") is room-form bookkeeping, not a pleading
            txt = re.split(r"\s*\.?\s*Must contain:", txt, 1)[0].rstrip(" .") + "."
            k = _key(txt)
            if k in seen_text:
                continue
            # S-near-dup (run 5): several lanes answer the same unit with the same paragraph (same rule quote, same record
            # facts, different wording) -> the court read the Note factors five times. Collapse by word-3-gram Jaccard;
            # the LONGER (more record-specific) text survives, deterministic, no LLM.
            sh = _shingles(txt)
            dup = next((pl for pl in pleads if _jaccard(sh, pl["sh"]) >= 0.55), None)
            if dup:
                if len(txt) > len(dup["text"]):
                    dup["text"], dup["sh"] = txt, sh
                    dup["pins"] = dup["pins"] + [x for x in p if x not in dup["pins"]]
                used.append(r)
                continue
            cites = {_cite_key(x["cite"]) for x in p if x["cite"]}
            if r["_origin"] != "room":
                host = next((pl for pl in pleads if pl["room"] and (cites & pl["cites"])), None)
                if host:               # same cite + unit: room row keeps the voice, m2 keeps the quote
                    for x in p:
                        if x["quote"] and (x["quote"], x["chunk"]) not in host["quotes"]:
                            host["quotes"].append((x["quote"], x["chunk"]))
                    used.append(r)
                    continue
            seen_text.add(k)
            pleads.append({"text": txt, "pins": p, "cites": cites, "quotes": [], "room": r["_origin"] == "room", "sh": sh})
            used.append(r)
        for pl in pleads:
            lines.append(_plead_line(pl["text"], pl["pins"]))
            for q, ch in pl["quotes"]:
                lines.append(f"  > {q} [corpus:{ch}]")
        if not pleads:
            zero.append(u["n"])
            lines.append("- (no GROUNDED, corpus-pinned row for this unit — see GAPS)")
        lines.append("")
    return lines, used, zero


def relief_items(rows: list, posture: dict | None) -> tuple:
    """-> (items, source). Remedy rows first (relief_item | relief clause of the proposition); else posture
    must_contain + vehicle lines, each ONLY when a GROUNDED pinned row carries that cite; else []."""
    items, seen = [], set()

    def add(txt, pins):
        t = _norm(txt).rstrip()
        if not t or _key(t) in seen:
            return
        seen.add(_key(t))
        tail = "; ".join(f"{p['cite']} [corpus:{p['chunk']}]" if p["cite"] else f"[corpus:{p['chunk']}]"
                         for p in pins if p["chunk"] not in t)
        items.append(t.rstrip(".") + (". " + tail if tail else ""))

    for r in rows:
        if not (_is_remedy(r) and _grounded(r)):
            continue
        pins = pins_of(r)
        if not pins:
            continue
        if r.get("relief_item"):
            add(r["relief_item"], pins)
            continue
        prop = _norm(r.get("proposition") or "")
        for s in _SENT_SPLIT.split(prop):
            if _is_relief_clause(s):
                add(s, pins)
                break
    if items:
        return items, "remedy rows"
    if posture:
        cited = set()
        for r in rows:
            if _grounded(r):
                for p in pins_of(r):
                    cited.add(_key(p["cite"]))          # full cite incl. subsection: 'parcp1028e'
        ppins = pins_of(posture)
        for mc in posture.get("must_contain") or []:
            add(mc, ppins)
        veh = _norm(str(posture.get("vehicle") or ""))
        if any("5505" in c for c in cited):
            add("vacate/rescind ORDER_17 under § 5505", [p for p in ppins if "5505" in (p["cite"] or "")])
        if any("1028e" in c for c in cited):
            add("grant leave to plead over per Pa.R.C.P. 1028(e)",
                [p for r in rows if _grounded(r) for p in pins_of(r) if "1028e" in _key(p["cite"])][:1])
        if items:
            return items, "posture row" + (f" ({veh})" if veh else "")
    return [], "none"


def vehicle_lines(posture: dict | None) -> list:
    if not posture:
        return ["VEHICLE: NOT DERIVED (no posture cell)"]
    pins = pins_of(posture)
    tail = "; ".join(f"{p['cite']} [corpus:{p['chunk']}]" for p in pins) if pins else "(no corpus pin on posture row)"
    mc = posture.get("must_contain") or []
    return [
        f"- **Vehicle:** {_norm(str(posture.get('vehicle') or 'NOT DERIVED'))}",
        f"- **Rule verbatim:** {_norm(str(posture.get('rule_verbatim') or 'NOT DERIVED'))} {tail}".rstrip(),
        f"- **Trigger:** {_norm(str(posture.get('trigger_event') or 'NOT DERIVED'))}"
        + (f" ({posture.get('trigger_date')})" if posture.get("trigger_date") else ""),
        f"- **Deadline rule:** {_norm(str(posture.get('deadline_rule') or 'NOT DERIVED'))}",
        f"- **Deadline date:** {_norm(str(posture.get('deadline_date') or 'NOT DERIVED'))}",
        f"- **Standard:** {_norm(str(posture.get('standard') or 'NOT DERIVED'))}",
        "- **Must contain:** " + ("; ".join(_norm(str(x)) for x in mc) if mc else "NOT DERIVED"),
    ]


def gap_rows(rows: list) -> list:
    """Every (unit, bot) that is NOT pleadable: status != GROUNDED, or GROUNDED without a corpus pin."""
    out = []
    for r in rows:
        if _is_posture(r):
            continue
        st = (r.get("status") or "").upper()
        if st == "GROUNDED" and pins_of(r):
            continue
        why = _norm(r.get("gap") or r.get("finding") or "")
        label = st if st != "GROUNDED" else "GROUNDED/UNPINNED (no corpus chunk on any authority)"
        fetch = [a.get("cite") for a in r.get("authorities") or [] if isinstance(a, dict) and (a.get("status") or "").upper() == "FETCH"]
        if fetch:
            why = (why + " " if why else "") + "FETCH (name resolves to no corpus pin; pull before pleading): " + "; ".join(_norm(c) for c in fetch)
        out.append((r["claim"] if isinstance(r["claim"], int) else 0, (r.get("bot") or ""), label, why, r["_origin"]))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _entered_from_rows(rows: list) -> str:
    c = collections.Counter()
    for r in rows:
        for m in re.finditer(r"entered\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})", (r.get("proposition") or "") + " " + (r.get("finding") or "")):
            c[m.group(1)] += 1
    return c.most_common(1)[0][0] if c else ""


# ------------------------------------------------------------------ NEW EVIDENCE BLOCK = record DOCUMENTS (S-exhibits-are-documents, 2026-10-25)
# Run 48 listed the movant's ARGUMENT as 17 'exhibits' (E1..E17 = the units' own record_facts prose). An exhibit is a record
# DOCUMENT: letter, title, date, [doc:<path>] that exists on disk — never a sentence from a unit or a cell. Two sources:
#   manifest  an exhibit manifest (default <out dir>/EXHIBITS.md, else the existing <out> stub's own block) whose rows already
#             carry the m1 shape '### <L> — <title>' / **Source:** … [doc:<path>] / **Quote:** > <verbatim from the document>;
#             a row is accepted ONLY when its [doc:] resolves on disk and its tag is a letter (E<n> unit rows are refused).
#   derived   every distinct record document the USED cells / units / timeline cite ([doc:<path>] tags, bare record/|intake/|
#             evidence/ paths) + the challenged order (record/orders/ORDER_<n>_<entered>.pdf). With a manifest, derived docs the
#             manifest lacks are LISTED (not lettered) so nothing enters the packet uncurated; without one they ARE the rows.
# Row shape is what pipeline.m1.parse_stub reads (### <tag> — <title> _(sev)_ / Element / Source / Quote) — unchanged.
_DOC_TAG = re.compile(r"\[\s*doc\s*:\s*([^\]\n]+?)\s*\]", re.I)
_REC_PATH = re.compile(r"(?<![\w/])((?:record|intake|evidence)/[\w./\-]+?\.(?:pdf|txt|md|csv|docx))\b", re.I)
_EX_ROW = re.compile(r"^###\s+([A-Z]\d?|E\d+)\s+[—-]\s+(.+?)\s*(?:_\((.*?)\)_)?\s*$(.*?)(?=^###\s+(?:E\d+|[A-Z]\d?)\s+[—-]|\Z)", re.M | re.S)


def _letters():
    for i in range(26):
        yield chr(65 + i)
    for i in range(26):
        for j in range(1, 10):
            yield f"{chr(65 + i)}{j}"


def resolve_doc(ref: str, bases: list) -> Path | None:
    """[doc:<ref>] -> the first existing file among <ref> as given / <base>/<ref> for each base (manifest dir, out dir,
    case dir, ROOT). A ref with several paths ('a.md; gmail 1a2b') is split on ';' and the first resolvable part wins."""
    for part in re.split(r"\s*;\s*", ref or ""):
        part = part.strip().strip("`'\"")
        part = re.sub(r"\s+§.*$", "", part)                      # 'evidence/x.md §1' -> the file
        if not part or " " in part:
            continue
        p = Path(part)
        cands = [p] if p.is_absolute() else [Path(b) / part for b in bases if b]
        for c in cands:
            if c.is_file():
                return c
    return None


def load_exhibit_manifest(path: Path, bases: list, stance: str = "amend") -> tuple:
    """-> (rows, refused). rows = [{tag, title, sev, body, doc(Path), raw}] in file order from the '## NEW EVIDENCE BLOCK'
    section (or the whole file when it has no '## ' sections). refused = [(tag, title, why)]."""
    if not path or not Path(path).is_file():
        return [], []
    t = Path(path).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^##\s*NEW EVIDENCE BLOCK[^\n]*\n(.*?)(?=^##\s|\Z)", t, re.S | re.M)
    sec = m.group(1) if m else t
    rows, refused = [], []
    for em in _EX_ROW.finditer(sec):
        tag, title, sev, body = em.groups()
        if re.fullmatch(r"E\d+", tag):
            refused.append((tag, _norm(title), "unit-prose tag E<n> (not a record document)"))
            continue
        if stance == "stand" and _AMEND_RX.search(title):
            refused.append((tag, _norm(title), "STANCE=stand: amendment exhibit (no Rule 1033 relief) — not in the packet"))
            continue
        src = re.search(r"\*\*Source:\*\*\s*(.+)", body)
        tags = _DOC_TAG.findall((src.group(1) if src else "") + " " + body)
        doc = None
        for ref in tags:
            doc = resolve_doc(ref, bases)
            if doc:
                break
        if not doc:
            refused.append((tag, _norm(title), "no [doc:<path>] on disk" if not tags else f"[doc:{tags[0]}] not on disk"))
            continue
        rows.append({"tag": tag, "title": _norm(title), "sev": (sev or "record").strip(), "body": body.rstrip(),
                     "doc": doc, "raw": em.group(0).rstrip()})
    return rows, refused


def derive_record_docs(rows_used: list, units_doc: dict, timeline: list, bases: list) -> list:
    """-> [(Path, how)] distinct record documents cited by the USED cells, the units and the timeline, challenged order first."""
    out, seen = [], set()

    def _stem_key(p: Path) -> str:
        return re.sub(r"(\.(pdf|docx))?\.(pdf|txt|md|csv|docx)$", "", p.resolve().name.lower())

    def add(ref, how):
        p = resolve_doc(ref, bases)
        if p and p.resolve() not in seen and _stem_key(p) not in seen:       # ORDER_17.txt is the OCR of ORDER_17.pdf, one row
            seen.add(p.resolve())
            seen.add(_stem_key(p))
            out.append((p, how))

    hdr = units_doc.get("header") or ""
    on = re.search(r"\bORDER_(\d+)\b", hdr)
    ent = units_doc.get("entered") or ""
    if on and ent:
        add(f"record/orders/ORDER_{on.group(1)}_{ent.replace('-', '')}.pdf", "challenged order (units header)")
    texts = [(hdr, "units header")] + [(u.get("body") or "", f"unit {u['n']}") for u in units_doc.get("units") or []]
    for r in rows_used:
        blob = " ".join(str(r.get(k) or "") for k in ("proposition", "finding", "record_fact", "rule_verbatim"))
        blob += " " + " ".join(str(x) for x in (r.get("record_facts") or []) if isinstance(x, str))
        texts.append((blob, f"U{r.get('claim')} {r.get('bot')} ({r.get('_origin')})"))
        for d in r.get("record_docs") or []:
            if isinstance(d, str):
                add(d, f"record_docs U{r.get('claim')} {r.get('bot')}")
    texts += [(txt, f"timeline {d}") for d, txt in timeline]
    for blob, how in texts:
        for ref in _DOC_TAG.findall(blob):
            add(ref, how)
        for ref in _REC_PATH.findall(blob):
            add(ref, how)
    return out


def _doc_date(p: Path) -> str:
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", p.name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") + " (file date; none in name)"


def _doc_title(p: Path) -> str:
    stem = re.sub(r"\.(pdf|docx)\.txt$", "", p.name)
    stem = re.sub(r"\.(pdf|txt|md|csv|docx)$", "", stem)
    stem = re.sub(r"^motion_EXHIBIT_[A-Z]\d?_", "", stem)
    return _norm(stem.replace("_", " "))


def _rel(p: Path, bases: list) -> str:
    for b in bases:
        try:
            return str(p.resolve().relative_to(Path(b).resolve()))
        except Exception:
            continue
    return str(p)


def humanize_order_labels(text: str, units_doc: dict) -> str:
    """BACKLOG S-order-label: the chain's internal handle (ORDER_17) is not a word a court reads. Map every bare
    ORDER_<n> of the units header to the forum's own words — "the Order entered <date> (Control No. <n>)" — leaving
    exhibit FILE names (ORDER_17_20261022.pdf) untouched. Deterministic guardrail, applied once at stub write."""
    _on = re.search(r"\bORDER_(\d+)\b", units_doc.get("header") or "")
    if not _on:
        return text
    n = _on.group(1)
    ent, cno = units_doc.get("entered"), units_doc.get("control_no")
    if not ent:
        return text
    full = f"the Order entered {ent}" + (f" (Control No. {cno})" if cno else "")
    # "ORDER_17 (Control No. 26010000, entered 2026-10-22)" -> full phrase once, no doubled parenthetical
    text = re.sub(r"\bORDER_" + n + r"\s*\((?:Control No\.?\s*\d+,?\s*)?(?:entered\s+\d{4}-\d{2}-\d{2})?\)", full, text)
    text = re.sub(r"\bORDER_" + n + r",\s*entered\s+\d{4}-\d{2}-\d{2}", full, text)
    # bare handle (not followed by _<digits> = exhibit file name)
    text = re.sub(r"\bORDER_" + n + r"\b(?!_\d)", full, text)
    # sentence-initial "the Order" -> "The Order"
    text = re.sub(r"(^|[.!?]\s+|\n)the Order entered", lambda m: m.group(1) + "The Order entered", text)
    return text


def _order_label(p: Path, units_doc: dict) -> str:
    """The forum's own words for the order under review ("Order challenged", Phila.Civ.R. *208.3(b)(3)(E)) when `p`
    IS that order (ORDER_<n> of the units header), whichever path cited it first. '' otherwise."""
    _m = re.search(r"ORDER_(\d+)_(\d{4})(\d{2})(\d{2})", p.name)
    _on = re.search(r"\bORDER_(\d+)\b", units_doc.get("header") or "")
    if _m and _on and _m.group(1) == _on.group(1):
        return f"Order challenged — ORDER_{_m.group(1)}, entered {_m.group(2)}-{_m.group(3)}-{_m.group(4)}"
    return ""


def _label_order_row(raw: str, doc: Path, units_doc: dict) -> str:
    """A manifest row whose title is only the raw filename (auto-derived in an earlier run) gets the forum label;
    a curated title (anything beyond the filename words) is left exactly as written."""
    lab = _order_label(doc, units_doc)
    m = re.match(r"^(###\s+[A-Z]\d?\s+—\s+)(.+?)(\s+_\([a-z]+\)_\s*)$", raw.split("\n", 1)[0])
    if not lab or not m:
        return raw
    if _norm(m.group(2)).lower() != _doc_title(doc).lower():
        return raw
    return raw.replace(m.group(0), m.group(1) + lab + m.group(3), 1)


def exhibit_rows(rows_used: list, units_doc: dict, timeline: list, out_path: Path, case_dir: Path, manifest: Path | None, stance: str = "amend") -> tuple:
    """-> (lines, n_rows, notes). Manifest rows verbatim (verified on disk) + derived docs; see the section comment above."""
    out_dir = Path(out_path).resolve().parent
    bases = [out_dir, case_dir, ROOT]
    notes, man_rows, src = [], [], None
    cands = [Path(manifest)] if manifest else [out_dir / "EXHIBITS.md", Path(out_path)]
    for c in cands:
        if c.is_file():
            rws, refused = load_exhibit_manifest(c, [c.resolve().parent] + bases, stance)
            for tag, title, why in refused:
                notes.append(f"manifest {c.name}: refused ### {tag} — {title[:70]}: {why}")
            if rws:
                man_rows, src = rws, c
                break
    derived = derive_record_docs(rows_used, units_doc, timeline, bases)

    def _sig(p: Path) -> str:                      # same bytes = same document (Exhibit A is a copy of record/orders/ORDER_17.pdf)
        import hashlib
        try:
            return hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            return str(p.resolve())

    covered = {r["doc"].resolve() for r in man_rows} | {_sig(r["doc"]) for r in man_rows}
    lines = []
    if man_rows:
        lines.append(f"<!-- exhibit manifest: {src} ({len(man_rows)} record documents, each [doc:] verified on disk) -->")
        for p, how in derived:
            if p.resolve() not in covered and _sig(p) not in covered:
                lines.append(f"- RECORD DOCUMENT cited by the cells but NOT in the exhibit manifest (curate before filing): "
                             f"[doc:{_rel(p, bases)}] — {how}")
        lines.append("")
        for r in man_rows:
            lines += [_label_order_row(r["raw"], r["doc"], units_doc), ""]
        if stance == "stand":
            # the packet tail reads a MANIFEST file, not the stub: write the stance-filtered manifest beside the source (PACKET_MANIFEST)
            sm = Path(src).with_name(Path(src).stem + f"_{stance}" + Path(src).suffix)
            sm.write_text("## NEW EVIDENCE BLOCK (STANCE=" + stance + f" — filtered from {Path(src).name}; amendment exhibits refused)\n\n"
                          + "\n\n".join(r["raw"] for r in man_rows) + "\n", encoding="utf-8")
            notes.append(f"stance manifest -> {sm} ({len(man_rows)} rows)")
        return lines, len(man_rows), notes
    lines.append("<!-- no exhibit manifest (EXHIBITS.md): rows derived from the record documents the cells/units/timeline cite -->")
    lines.append("")
    n = 0
    for letter, (p, how) in zip(_letters(), derived):
        n += 1
        _title = _doc_title(p)
        _title = _order_label(p, units_doc) or _title
        lines += [f"### {letter} — {_title} _(record)_",
                  f"**Element:** record document cited by {how}",
                  f"**Date:** {_doc_date(p)}",
                  f"**Source:** {p.name} [doc:{_rel(p, bases)}]",
                  ""]
    if not n:
        lines.append("- (no record document cited by the used cells / units / timeline, and no EXHIBITS.md manifest)")
    return lines, n, notes


def build_stub(rows, counts, units_doc, timeline, case, court, ftype, out_path=None, exhibits=None, stance="amend",
               counts_label="", order_grounds=True, learnings=None) -> tuple:
    units = units_doc["units"]
    global STANCE_FTYPE; STANCE_FTYPE = ftype
    rows, dropped = stance_filter(rows, stance)
    posture = next((r for r in rows if _is_posture(r)), None)
    if stance == "stand" and posture and not _cites_key(posture):
        posture = None                                 # a non-§ 5505 posture row is not the vehicle the stance names
    title = TITLE_OF.get(ftype, ftype.replace("_", " ").title())
    # ---- S-avoidance-order (2026-11-14, class-level): the grounds are emitted NARROW -> BROAD and the
    #   constitutional ground is ALWAYS LAST, as a BACKSTOP. Not strategy — the court's own canon
    #   ([corpus:cap_us_557_193_northwest_austin_municipal_utility_distr#15d24e41ff] / Ashwander; PA:
    #   [corpus:scotus_stilp_v_commonwealth_905a2d918#165032b384]). Deterministic guardrail = the class floor from
    #   each unit's own authority family + "backstop cites a GROUNDED constitutional authority or is OPEN+PULL:".
    order = None
    if order_grounds:
        try:
            _go = _sibling_organ("ground_order.py", "ground_order")
            order = _go.compose(units, rows, forum=court, case=case, pins_of=pins_of, grounded=_grounded,
                                learnings=learnings)
            seq = {g["unit"]: g for g in order["ordered"]}
            units = sorted(units, key=lambda u: seq.get(u["n"], {}).get("seq", 10 ** 6))
            units_doc = {**units_doc, "units": units}
        except Exception as e:                       # a gate that dies must say so, never silently emit unordered grounds
            order = {"error": f"{type(e).__name__}: {e}", "ordered": [], "backstop": {}, "lines": [], "check": {}}
    spine, used, zero = build_spine(rows, units, labels={g["unit"]: g for g in (order or {}).get("ordered") or []})
    if order and (order.get("backstop") or {}).get("status") == "PLEAD":
        bs = order["backstop"]
        spine += [f"### {_go_backstop_heading(bs)}",
                  f"- GROUND {len(order['ordered']) + 1} [class d — constitutional backstop] (ALWAYS LAST; "
                  f"derived by {bs['source']})",
                  _plead_line(bs["text"], [{"cite": a["cite"], "chunk": a["chunk"], "quote": a.get("quote") or ""}
                                           for a in bs["authorities"]]),
                  ""]
    relief, relief_src = relief_items(rows, posture)
    if stance == "stand":
        relief = [it for it in relief if not _AMEND_RX.search(it)]
        _ex = [re.compile(x, re.I) for x in _vehicle().get("exclude") or []]     # vehicle.exclude_relief (filing-type YAML)
        _foreign = [it for it in relief if any(r.search(it) for r in _ex)]
        if _foreign:
            STANCE_DROPPED.extend(("relief-foreign", {"relief_item": it}) for it in _foreign)
            relief = [it for it in relief if it not in _foreign]
        added = stance_relief(rows, relief, units_doc, counts_label)
        if added:
            core = [it for it in added if "2.11" not in it]; dq = [it for it in added if "2.11" in it]
            relief = core + relief + dq                # vacate / reinstate first, the lanes' items, disqualification last
            relief_src = (relief_src if relief else "none") + f" + stance=stand ({len(added)} item(s) on GROUNDED pins)"
    gaps = gap_rows(rows)
    entered = units_doc["entered"] or _entered_from_rows(rows) or "NOT IN RECORD"

    L = [f"# DRAFT - {title}", "",
         f"**Court:** {court}  ",
         f"**Case:** {case}  ",
         f"**Filing type:** {ftype}  ",
         f"**Control No.:** {units_doc['control_no'] or 'NOT IN RECORD'}  ",
         f"**Order entered:** {entered}  ",
         f"**Order docketed:** {units_doc['docketed'] or 'NOT IN RECORD'}  ",
         f"**Generated:** {_dt.datetime.now().isoformat(timespec='seconds')}  ",
         "**Source cells:** " + "; ".join(f"{p} ({n} rows)" for p, n in counts.items()) + "  ",
         f"**Units:** {Path(units_doc.get('_path', 'ADVERSE_POINTS.md')).name} ({len(units)} units)  ",
         f"**Stance:** {stance}  ",
         "", "## CAPTION", "",
         f"Movant: Plaintiff, sui juris (the party against whom the order under review, Control No. "
         f"{units_doc['control_no'] or 'NOT IN RECORD'}, was entered). Court: {court}. Case: {case}.",
         "", "## VEHICLE", ""]
    L += vehicle_lines(posture)
    if stance == "stand":
        L.append("- **Stance:** the complaint STANDS as pleaded — no amendment relief, no Rule 1033 vehicle; vehicle = 42 Pa.C.S. § 5505 "
                 "(modify or rescind within 30 days of entry); relief = vacate, reinstate the dismissed counts as pleaded, answer in 20 days, "
                 "and Rule 2.11(A) disqualification/reassignment where the impartiality unit is sustained")
    # S-avoidance-order: the ORDERING is visible BEFORE the spine (numbered grounds with their class a-d, the
    # backstop last) so the m1 gate can check it mechanically and the drafter reads it before it reads a ground.
    if order and (order.get("lines") or order.get("error")):
        L += [""] + (order.get("lines") or
                     ["## GROUNDS (ordering — constitutional avoidance)", "",
                      f"- ORDERING NOT COMPUTED: {order.get('error')} — grounds below are in unit order, NOT narrow->broad."])
    L += ["", "## CONTROLLING AUTHORITY (verbatim pleads from GROUNDED, corpus-pinned cells; one heading per unit)", ""]
    L += spine
    L += ["## RELIEF SHAPE" + (f" (from {relief_src})" if relief else ""), ""]
    for i, it in enumerate(relief, 1):
        L.append(f"{i}. {it}")
    if not relief:
        L.append("(no relief item derivable from the cells — no GROUNDED remedy row and no posture row)")
    L += ["", "## Administrative history (PROCEDURAL HISTORY — CASE_TIMELINE, chronological)", ""]
    for d, txt in timeline:
        L.append(f"- {d}: {_court_facing_event(txt)}")
    if not timeline:
        L.append("- (CASE_TIMELINE has no entries)")
    L += ["", "## NEW EVIDENCE BLOCK (EVIDENCE — record documents; letter / title / date / [doc:<path>] — never unit prose)", ""]
    ex_lines, ev, ex_notes = exhibit_rows(used, units_doc, timeline, Path(out_path or "stub.md"), CASES / case, exhibits, stance)
    L += ex_lines
    L += ["## GAPS (not pleaded)", ""]
    for n, bot, label, why, origin in gaps:
        L.append(f"- Unit {n} — {bot} ({origin}) [{label}]: {why or '(no gap text)'}")
    if not gaps:
        L.append("- none")
    for kind, r in dropped:
        L.append(f"- STANCE=stand: dropped {kind} row U{r.get('claim')} {r.get('bot')} ({r.get('_origin')}) — amendment relief/vehicle: "
                 f"{_norm(str(r.get('relief_item') or r.get('vehicle') or r.get('proposition') or ''))[:160]}")
    # S-cite-drift-normalize: every FETCH authority (case name with no corpus pin) is listed here for the fetch bot / the
    # next pull — even when its row pleads on another, pinned authority. Never a pin tail, never a CASE_FACTS authority.
    fetch = collections.OrderedDict()
    for r in rows:
        for a in r.get("authorities") or []:
            if isinstance(a, dict) and (a.get("status") or "").upper() == "FETCH":
                fetch.setdefault(_norm(a.get("cite") or ""), []).append(f"U{r['claim']} {r.get('bot')} ({r['_origin']})")
    for cite, where in fetch.items():
        L.append(f"- FETCH — {cite} [{len(where)} row(s): {', '.join(where[:4])}{'…' if len(where) > 4 else ''}]: name resolves to no corpus pin; pull (Harvard CAP first) before pleading")
    L.append("")
    report = {"units": len(units), "zero_units": zero, "pleads": sum(1 for x in spine if x.startswith("- PLEAD")),
              "used_rows": len(used), "relief": len(relief), "relief_src": relief_src, "gaps": len(gaps),
              "posture": bool(posture), "evidence": ev, "evidence_notes": ex_notes, "timeline": len(timeline),
              "stance": stance, "stance_dropped": [(k, r.get("claim"), r.get("bot")) for k, r in dropped],
              "grounded_rows": [r for r in used],
              "order": {"ordered": [(g["seq"], g["class"], g["unit"]) for g in (order or {}).get("ordered") or []],
                        "check": (order or {}).get("check") or {},
                        "backstop": {k: v for k, v in ((order or {}).get("backstop") or {}).items()
                                     if k in ("status", "interest", "source", "pull", "units")},
                        "error": (order or {}).get("error") or "",
                        "llm": (order or {}).get("llm_log") or []}}
    return "\n".join(L), report


# ------------------------------------------------------------------ CASE_FACTS write-back (reuses the organ writer)
def write_case_facts(cf_path: Path, grounded_rows: list, filing: str = "") -> bool:
    """Upsert the GROUNDED, pinned propositions into '## M2 LANE FINDINGS' via pipeline.m2.organ.runner.cf_block +
    case_facts_refresh._upsert_block (the one writer). Lane name = 'cells_to_stub'. Backup first."""
    from pipeline.m2.organ.runner import cf_block, CF_HEADER   # noqa: E402
    from pipeline.case_facts_refresh import _upsert_block       # noqa: E402
    # S-cf-history (2026-11-12, user lock): one dated ### block PER FILING, never one shared block — the old single
    # "cells_to_stub" lane upserted by claim label (U1, U2 …) so a later filing's U1 silently kept the EARLIER filing's line.
    # CASE_FACTS is the history tracker: every adverse paper gets its full analysis appended under its own heading.
    import datetime as _dt
    lane = f"cells_to_stub · {filing} · {_dt.date.today().isoformat()}" if filing else "cells_to_stub"
    cells = []
    for r in grounded_rows:
        pins = pins_of(r)
        a0 = pins[0] if pins else None
        cells.append({"claim": f"U{r['claim']} {r.get('bot')} ({r['_origin']})", "status": "GROUNDED", "m0": "n/a",
                      "finding": _norm(r.get("finding") or ""), "proposition": _norm(r.get("proposition") or ""),
                      "authorities": [{"quote": a0["quote"], "cite": a0["cite"], "source": a0["chunk"]}] if a0 else []})
    doc = cf_path.read_text(encoding="utf-8", errors="ignore") if cf_path.exists() else ""
    m = re.search(rf"^{re.escape(CF_HEADER)}\n(.*?)(?=^## |\Z)", doc, re.S | re.M)
    existing = m.group(1) if m else ""
    # S-cite-drift-normalize: cf_block is incremental (a claim label already present keeps its OLD line). The newest GROUNDED
    # row per (origin, bot, claim) wins here, so a stale line for a claim we are writing now (e.g. one carrying a drifted
    # cite from a pre-fix run) is dropped from this lane's block first — the writer and the block format stay the organ's.
    now = {c["claim"] for c in cells}
    sec = re.search(rf"^### {re.escape(lane)}\n(.*?)(?=^### |\Z)", existing, re.S | re.M)
    if sec:
        kept = [ln for ln in sec.group(1).splitlines() if not (ln.count("**") >= 2 and ln.split("**")[1] in now)]
        existing = existing[: sec.start(1)] + "\n".join(kept) + ("\n" if kept else "") + existing[sec.end(1):]
    blk = cf_block(existing, lane, cells)
    body = re.sub(rf"^### {re.escape(lane)}\n.*?(?=^### |\Z)", "", existing, flags=re.S | re.M).rstrip() + "\n\n" + blk
    if cf_path.exists():
        bak = cf_path.with_suffix(f".md.bak.{time.strftime('%Y%m%d_%H%M%S')}")
        bak.write_text(doc, encoding="utf-8")
    return _upsert_block(cf_path, CF_HEADER, CF_HEADER + "\n" + body.strip() + "\n")


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="deterministic cells -> m0-shaped stub (m1 application lane input)")
    ap.add_argument("--cells", nargs="+", required=True, help="one or more cells .jsonl (m2 + room)")
    ap.add_argument("--units", required=True, help="ADVERSE_POINTS.md (unit titles, header, record facts)")
    ap.add_argument("--case", required=True)
    ap.add_argument("--court", default="PA_CP_PHILA")
    ap.add_argument("--type", dest="ftype", default="motion_to_vacate")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeline", default=None, help="CASE_TIMELINE.json (default: cases/<case>/CASE_TIMELINE.json)")
    ap.add_argument("--write-cf", action="store_true")
    ap.add_argument("--allow-open", action="store_true",
                    help="S-room-closure: write the stub even though a unit has no GROUNDED/RECORD row (default: exit 3 and name them)")
    ap.add_argument("--case-facts", default=None, help="CASE_FACTS.md to upsert (default: cases/<case>/CASE_FACTS.md)")
    ap.add_argument("--no-order", action="store_true",
                    help="S-avoidance-order: emit the grounds in UNIT order instead of narrow->broad with the "
                         "constitutional backstop last (the canon is ON by default; this says so loudly in the run log)")
    ap.add_argument("--learnings", default=None,
                    help="append the avoidance ORDERING/CONSTRUCTION pattern notes to THIS learnings.jsonl "
                         "(default: output/learnings.jsonl; a dry run should point it at a scratch file)")
    ap.add_argument("--dry", action="store_true",
                    help="compute and PRINT the ordered ground list + backstop; write nothing (read-only probe)")
    ap.add_argument("--stance", default=os.environ.get("STANCE", "amend"), choices=["amend", "stand"],
                    help="the user's amend-vs-stand decision (env STANCE); stand = drop amendment rows/items/exhibits, § 5505 vehicle")
    ap.add_argument("--counts", default=os.environ.get("COUNTS", ""), help="dismissed counts label for the stance relief items (env COUNTS)")
    ap.add_argument("--exhibits", default=None,
                    help="exhibit manifest for the NEW EVIDENCE BLOCK (### <L> — title / **Source:** … [doc:<path>] rows); "
                         "default: <out dir>/EXHIBITS.md, else the existing <out> stub's own block; else rows are derived from the "
                         "record documents the cells/units/timeline cite")
    a = ap.parse_args(argv)

    for p in list(a.cells) + [a.units]:
        if not Path(p).is_file():
            sys.exit(f"cells_to_stub: input not found: {p}")
    rows, counts = load_cells(a.cells)
    for res in NORMALIZE_LOG:
        if res["action"] == "normalized":
            print(f"  [cite-normalize] normalized {res['old']} -> {res['new']} [{res['source']}] (U{res['claim']} {res['bot']} {res['origin']})")
        else:
            print(f"  [cite-normalize] FETCH {res['old']} (U{res['claim']} {res['bot']} {res['origin']}): {res['why']}")
    units_doc = load_units(a.units)
    units_doc["_path"] = a.units
    if not units_doc["units"]:
        sys.exit(f"cells_to_stub: no '**N. <title>**' units in {a.units}")
    global CASE_SLUG
    CASE_SLUG = a.case                                   # the adverse-party names come from this
    tl = a.timeline or str(CASES / a.case / "CASE_TIMELINE.json")
    timeline = load_timeline(tl)

    # ---- S-room-closure (2026-11-13, BACKLOG "ROOM — run-to-closure ledger"): a stub is a FILING. An unanalyzed unit is a
    #   hole in the filing, so an UNCLOSED ledger is refused here (exit 3) and the chain re-opens the room on those units.
    #   CLOSED = the unit has at least one GROUNDED/RECORD row in the ledger. --allow-open ships anyway (the user's call) and
    #   says so in the run log. Nothing is written on a refusal — the old stub on disk stays the stub of record.
    CLOSING = {"GROUNDED", "RECORD"}
    def _cn(v):
        try: return int(str(v).strip())
        except Exception: return None
    closed = {_cn(r.get("claim")) for r in rows if str(r.get("status") or "").upper() in CLOSING}
    open_units = [u["n"] for u in units_doc["units"] if u["n"] not in closed]
    if open_units:
        where = Path(a.out).parent.parent / "COVERAGE.md"
        msg = (f"cells_to_stub: UNCLOSED LEDGER — {len(open_units)} of {len(units_doc['units'])} unit(s) have no GROUNDED/RECORD "
               f"row: {open_units} (units file {a.units}; ledger {', '.join(str(c) for c in a.cells)})")
        if not a.allow_open:
            print(msg)
            print(f"cells_to_stub: REFUSING to write {a.out} — re-open the room on unit(s) "
                  f"{','.join(str(n) for n in open_units)} (ROOM_UNITS=<those units>), or pass --allow-open to ship over them."
                  + (f" Coverage: {where}" if where.exists() else ""))
            return 3
        print(msg + " — --allow-open: writing the stub over the open unit(s)")
    text, rep = build_stub(rows, counts, units_doc, timeline, a.case, a.court, a.ftype,
                           out_path=Path(a.out), exhibits=Path(a.exhibits) if a.exhibits else None, stance=a.stance,
                           counts_label=a.counts, order_grounds=not a.no_order,
                           learnings=(Path(a.learnings) if a.learnings else None))
    out = Path(a.out)
    if a.dry:
        # read-only probe: the ORDERED ground list + the backstop, nothing written (never a live drafts dir)
        print("cells_to_stub --dry: GROUNDS (ordering — constitutional avoidance)")
        inside = False
        for ln in text.splitlines():
            if ln.startswith("## GROUNDS"):
                inside = True
            elif inside and ln.startswith("## ") and not ln.startswith("## GROUNDS"):
                break
            if inside:
                print("  " + ln)
        o = rep.get("order") or {}
        print("  -- order check: " + json.dumps(o.get("check") or {}, ensure_ascii=False))
        print("  -- backstop:    " + json.dumps(o.get("backstop") or {}, ensure_ascii=False)[:900])
        for ln in o.get("llm") or []:
            print("  -- [llm] " + ln)
        print(f"  -- DRY: nothing written (would have been {out})")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    text = humanize_order_labels(text, units_doc)
    # S-room-depth: the room's RULE-QUESTION clause ('QUESTION: Does Rule 3146(a) not say “…”? …') is court prose; its form
    # label is room bookkeeping — drop the label, keep the question. Only the new label; no other text changes.
    text = re.sub(r"(?<![A-Za-z])QUESTION:\s*", "", text)
    out.write_text(text, encoding="utf-8")

    print(f"cells_to_stub: {len(rows)} newest rows from {len(counts)} file(s); units={rep['units']} "
          f"pleads={rep['pleads']} used_rows={rep['used_rows']} evidence={rep['evidence']} timeline={rep['timeline']}")
    for n in rep.get("evidence_notes") or []:
        print(f"  [exhibits] {n}")
    if rep["zero_units"]:
        print(f"  units with ZERO grounded pinned rows: {rep['zero_units']}")
    print("  VEHICLE: " + ("derived from posture row" if rep["posture"] else "NOT DERIVED (no posture cell)"))
    if rep.get("stance") == "stand":
        print(f"  STANCE=stand: dropped {len(rep['stance_dropped'])} amendment row(s) {rep['stance_dropped']}")
    if rep["relief"]:
        print(f"  RELIEF SHAPE: {rep['relief']} item(s) from {rep['relief_src']}")
    else:
        print("RELIEF SHAPE: 0 items — m1 will refuse")
    print(f"  GAPS (not pleaded): {rep['gaps']} unit/bot pair(s)")
    print(f"  stub -> {out}")

    if a.write_cf:
        cf = Path(a.case_facts) if a.case_facts else CASES / a.case / "CASE_FACTS.md"
        ok = write_case_facts(cf, rep["grounded_rows"], filing=a.ftype)
        print(f"  CASE_FACTS '## M2 LANE FINDINGS' upserted={ok} -> {cf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
