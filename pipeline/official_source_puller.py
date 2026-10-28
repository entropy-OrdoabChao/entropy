#!/usr/bin/env python3
"""official_source_puller — corpus-first, official-publisher ingest gate.

Enforces the LOCKED sequence from
obsidian_sync/playbooks/corpus-ingest-discipline.md  (card t_562a9556,
user-corrected 2026-09-17). The discipline, in order:

  1. CORPUS-FIRST sweep ALL families. ANY hit = STOP, cite the existing key,
     NO pull.  Three independent channels, any one is decisive:
       - left-anchored family key sweep  (source LIKE '<family>%')
       - distinctive-phrase FTS MATCH    (the text arbiter — dedupe cuts both ways)
       - semantic multi-angle            (embeddings find paraphrase)
  2. Only a TRUE all-families miss proceeds to the OFFICIAL PUBLISHER whitelist
     (the institution that OWNS the text). A host not on the whitelist fails
     step 2 — refuse by default. Never commentary, never a law-blog/wiki copy.
  3. Ingest per the legal-corpus-ingest contract: raw saved, BOTH tables
     written, count + operative-phrase probe verified, conservation check,
     manifest row. No success without the probe hitting.
  4. Close the loop: the document that flagged NOT-IN-CORPUS gets updated from
     the hedge to [corpus:<key>].

The check-first gate cannot be bypassed except with --force PLUS
--checked-against <families> AND --reason. A --force without a family list
exits non-zero: an unbypassed check is a discipline, a silently-bypassed check
is a ritual. Every force is logged with a timestamp for a skeptic re-audit.

Usage:
  # step-1 gate (default action) — the workhorse:
  python3 -m pipeline.official_source_puller check --family pa_cs_t13 \\
      --phrase "shall be published" --query "PA title 13 definitions"

  # full pull: check -> whitelist -> fetch -> ingest -> probe -> manifest
  python3 -m pipeline.official_source_puller pull --source phila_ccp_local_1028c \\
      --url https://www.phila.gov/.../local-rules.pdf \\
      --kind federal_court_rules --family phila \\
      --phrase "<operative-words-of-the-rule>" \\
      --jurisdiction PA_CC_PHILA --cite "Phila.Civ.R. 1028(c)"

  # inspect the force log
  python3 -m pipeline.official_source_puller state

A NOT-IN-CORPUS verdict from a single family is not absence — the gate sweeps
ALL families and the phrase arbiter before it will let a pull proceed.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
# Canonical corpus path (same import the rest of the pipeline uses).
try:
    from pipeline.turbovec_ingest import CHUNK_DB
except Exception:  # pragma: no cover
    CHUNK_DB = (_eroot() / "corpus/chunk_text.db")

# Canonical chunking (paragraph -> sentence, MERGE short tail, assert-on-output shape).
try:
    from pipeline.case_fetch import _chunks, _clean
except Exception:  # pragma: no cover
    _chunks, _clean = None, None

CORPUS_API = "http://localhost:8100/search"
STATE = ROOT / "output" / "plaw_backfill" / "official_source_puller_state.json"
MANIFEST = ROOT / "output" / "plaw_backfill" / "official_source_puller_manifest.jsonl"
RAW_ROOT = ROOT / "corpus_sources"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36")

# The families a NOT-IN-CORPUS sweep must cover (left-anchored source keys).
# A check with no --family given sweeps ALL of these by default — that is the
# "sweep ALL families" requirement. A single-family miss is NOT absence.
ALL_FAMILY_PREFIXES = [
    "cap_us_", "SCOTUS_vol_", "usc_", "cfr_", "pa_", "pa_code_", "pa_cs_",
    "courtlistener_", "ca1_", "ca2_", "ca3_", "ca4_", "ca5_", "ca6_", "ca7_",
    "ca8_", "ca9_", "ca10_", "ca11_", "ca12_", "ca13_", "cafc_",
    "senate_report_", "doi_", "sec_", "uscourts_", "phila_",
]

# Official-publisher whitelist (playbook step 2). host-substring patterns per
# authority kind. A pull whose origin host matches none of the kind's patterns
# (and is not --forced) is refused: record which official publisher OWES it
# instead of pulling from commentary.
OFFICIAL_PUBLISHERS = {
    "us_code": ["uscode.house.gov", "govinfo.gov"],
    "cfr_reg": ["ecfr.gov", "govinfo.gov"],
    "federal_court_rules": [".uscourts.gov", "uscourts.gov", "federalreserve.gov"],
    "scotus": ["supremecourt.gov", "static.case.law"],
    "pa_consolidated": ["legis.state.pa.us", "palegis.us"],
    "pa_rules": ["pocodeandbulletin.gov", "pacodeandbulletin.gov"],
    "pa_court_rules": ["pacourts.us"],
    "agency_manual": [".gov", "msrb.org"],
    "treasury": ["fiscal.treasury.gov"],
    "philadelphia": ["phila.gov"],
    # GENERIC kinds — the state-pack vocabulary (entropy/pipeline/state_pack_ingest.KIND). Government-published only:
    # any .gov host, any state-government host (.state.<st>.us / .<st>.us), any court or legislature host. A state whose
    # official code is vendor-hosted (non-government domain) must mark the pack row `official_publisher: true`; the
    # pack ingester then pulls it --force with the pack's vetting status as the reason. Nothing else passes.
    "constitution": [".gov", ".state.", ".us", "courts.", "legis", "legislature."],
    "statute": [".gov", ".state.", ".us", "legis", "legislature.", "capitol."],
    "regulation": [".gov", ".state.", ".us", "sos.", "register."],
    "register": [".gov", ".state.", ".us", "sos.", "register."],
    "court_rule": [".gov", ".state.", ".us", "courts.", "uscourts.gov", "judiciary."],
    "court_form": [".gov", ".state.", ".us", "courts.", "uscourts.gov", "judiciary."],
    "opinion_index": [".gov", ".state.", ".us", "courts.", "judiciary.", "static.case.law", "courtlistener.com"],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------- channels
def _semantic(queries, n=3):
    """POST the corpus semantic API. Returns a list of {source, text} hits, or
    [] on a down/error API (an API error is NOT a miss — the caller says so)."""
    if not queries:
        return []
    try:
        req = urllib.request.Request(
            CORPUS_API,
            data=json.dumps({"queries": queries, "n_results": n}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return [{"api_error": f"{type(e).__name__}: {str(e)[:120]}"}]
    out = []
    results = (data or {}).get("results", {})
    for q, hits in results.items():
        for h in (hits or [])[:n]:
            out.append({"query": q, "term": h.get("term"),
                        "text": (h.get("text") or "")[:240]})
    return out


def _fts_match(con, phrase):
    """The fast text arbiter: is this distinctive phrase held ANYWHERE?
    FTS columns are (text, source_id) — there is no `source` column."""
    p = (phrase or "").strip().replace('"', " ")
    if not p:
        return []
    try:
        rows = con.execute(
            "SELECT source_id, substr(text,1,160) FROM chunk_text_fts "
            "WHERE chunk_text_fts MATCH ? LIMIT 5",
            ('"' + p + '"',)).fetchall()
    except Exception as e:
        return [{"fts_error": f"{type(e).__name__}: {str(e)[:120]}"}]
    return [{"source_id": r[0], "text": r[1]} for r in rows]


# --------------------------------------------------------------------- gate
def corpus_first_check(families=None, phrase=None, queries=None, db=None):
    """Step 1: sweep ALL families. Returns a dict; present=True if ANY channel
    hit. This is the check-first gate — a pull must not proceed when present."""
    db = str(db or CHUNK_DB)
    if not Path(str(db)).exists():                       # fresh home: no store yet → nothing is in the corpus
        return {"present": False, "verdict": "MISSING", "checked_families": list(families or ALL_FAMILY_PREFIXES),
                "family_hits": {}, "fts_hits": [], "semantic_hits": [], "api_down": False, "fts_error": False,
                "ts": _now(), "detail": "no chunk store yet (fresh home)"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=30000")
    fams = families if families else ALL_FAMILY_PREFIXES
    family_hits = {}
    for f in fams:
        n = con.execute(
            "SELECT COUNT(*) FROM chunk_text WHERE source LIKE ?||'%'",
            (f,)).fetchone()[0]
        if n:
            family_hits[f] = n
    fts_hits = _fts_match(con, phrase)
    con.close()
    sem_hits = _semantic(queries) if queries else []

    api_down = any(h.get("api_error") for h in sem_hits)
    fts_err = any(h.get("fts_error") for h in fts_hits)
    present = bool(family_hits or fts_hits or
                   any(not h.get("api_error") for h in sem_hits))
    verdict = "PRESENT" if present else ("MISSING" if (fams) else "NO_FAMILIES")
    return {
        "present": present,
        "verdict": verdict,
        "checked_families": fams,
        "family_hits": family_hits,
        "fts_hits": fts_hits,
        "semantic_hits": [h for h in sem_hits if not h.get("api_error")],
        "api_down": api_down,
        "fts_error": fts_err,
        "ts": _now(),
    }


def cite_existing(check):
    """The 'STOP, cite the existing key' payload for a PRESENT verdict."""
    cites = []
    for fam, n in (check.get("family_hits") or {}).items():
        cites.append(f"[corpus:{fam}_* ({n} chunks)]")
    for h in (check.get("fts_hits") or []):
        if h.get("source_id"):
            cites.append(f"[corpus:fts {h['source_id']}]")
    for h in (check.get("semantic_hits") or []):
        cites.append(f"[corpus:semantic ~{(h.get('term') or '')[:40]}]")
    return cites or ["[corpus:present — re-probe the family that hit]"]


# ---------------------------------------------------------------- escape hatch
def verify_force_evidence(force, checked, reason, swept_families):
    """The ONLY bypass: --force PLUS --checked-against <families> AND --reason.
    Returns (ok, reason-string). A force without a family list is a ritual,
    not a discipline — it exits non-zero."""
    if not force:
        return (True, "not forced")
    if not checked:
        return False, ("--force requires --checked-against <families>: the "
                       "families actually swept. An unbypassed check is a "
                       "discipline; a silently-bypassed check is a ritual.")
    if not (reason or "").strip():
        return False, "--force requires --reason (why the sweep missed / why pull anyway)."
    missing = [f for f in checked if f not in (swept_families or [])]
    if missing:
        return False, (f"--checked-against names families not in this sweep: "
                       f"{missing}. Re-run the sweep across them first.")
    return True, "force evidence verified"


def log_force(entry):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if STATE.exists():
        try:
            rows = json.loads(STATE.read_text("utf-8"))
        except Exception:
            rows = []
    entry["ts"] = _now()
    rows.append(entry)
    STATE.write_text(json.dumps(rows, indent=1), "utf-8")
    return entry


# ------------------------------------------------------------- publisher gate
def resolve_publisher(url, kind):
    """Step 2: does the origin host belong to the authority kind's official
    publisher? Returns (allowed, host, note)."""
    host = ""
    if url:
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            host = (url or "").lower()
    pats = OFFICIAL_PUBLISHERS.get(kind)
    if pats is None:
        return False, host, (f"unknown --kind {kind!r}; allowed: "
                             f"{sorted(OFFICIAL_PUBLISHERS)}")
    for p in pats:
        if p.startswith("."):
            if host.endswith(p) or host == p.lstrip(".") or host in p.lstrip("."):
                return True, host, f"whitelisted ({p}) for {kind}"
        elif p in host or host in p:
            return True, host, f"whitelisted ({p}) for {kind}"
    return False, host, (f"{host or '(no host)'} not an official publisher for "
                         f"{kind} {pats}; record who OWES it, do not pull commentary")


# ------------------------------------------------------------------ ingest
def _chunk_id(source, n):
    # Match the DB's dominant convention (sha1[:16]); the embed delta reads
    # chunk_id as the row key.
    return hashlib.sha1(f"{source}:{n}".encode("utf-8")).hexdigest()[:16]


def _delete_source(con, source):
    """Convention-AGNOSTIC idempotent delete. Grab the source's chunk_ids from
    chunk_text, remove exactly those from FTS, then the chunk_text rows. A
    pattern-anchored FTS delete (source_id LIKE src||'_chunk_%') would orphan
    stale FTS rows on the dominant sha1-hex convention — never do that."""
    old_ids = [r[0] for r in con.execute(
        "SELECT chunk_id FROM chunk_text WHERE source=?", (source,))]
    if old_ids:
        con.executemany("DELETE FROM chunk_text_fts WHERE source_id=?",
                        [(i,) for i in old_ids])
    con.execute("DELETE FROM chunk_text WHERE source=?", (source,))


def conservation_ratio(source_text, chunks):
    """Non-whitespace char count of source ~= sum of chunks. A lossy write
    (discarded tail) aborts the ingest."""
    a = len(re.sub(r"\s", "", source_text or ""))
    b = sum(len(re.sub(r"\s", "", c)) for c in chunks)
    if not a:
        return 0.0
    return b / a


def _ensure_store(con):
    """Identical schema to the baseline chunk store (chunk_text WITHOUT ROWID + fts5 porter/unicode61). No-op when present."""
    con.execute("CREATE TABLE IF NOT EXISTS chunk_text (chunk_id TEXT PRIMARY KEY, text TEXT, source TEXT, "
                "jurisdiction TEXT, citations TEXT) WITHOUT ROWID")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ct_source ON chunk_text(source)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_text_fts USING fts5(text, source_id UNINDEXED, "
                "tokenize='porter unicode61')")


def ingest_source(source, text, jurisdiction="US", cite="", phrase="",
                  family="", origin_url="", raw_path=None, db=None,
                  dry=False):
    """Step 3: the legal-corpus-ingest contract. Returns a result dict; ok=False
    when the probe misses or conservation fails (nothing committed)."""
    res = {"ok": False, "source": source, "chunks": 0, "probe_hits": None,
           "conservation": None, "dry": dry, "ts": _now(), "detail": ""}
    if _chunks is None:
        res["detail"] = "case_fetch chunker unavailable (import failed)"
        return res
    cleaned = _clean(text)
    chunks = _chunks(cleaned, 1400) if cleaned else []
    if not chunks:
        res["detail"] = "no chunks produced (empty text)"
        return res
    res["chunks"] = len(chunks)
    assert max(len(c) for c in chunks) <= 1500, "chunk cap breached on output"

    ratio = conservation_ratio(cleaned, chunks)
    res["conservation"] = round(ratio, 4)
    if ratio < 0.85:
        res["detail"] = (f"conservation check FAILED ({ratio:.3f}): "
                         f"chunk sum lost vs source; aborting a lossy write")
        return res

    # raw saved (the user reads originals)
    if not dry:
        rawdir = RAW_ROOT / (family or "official_sources")
        rawdir.mkdir(parents=True, exist_ok=True)
        rp = Path(raw_path) if raw_path else rawdir / f"{source}.txt"
        if not raw_path:
            rp.write_text(cleaned, "utf-8")
        res["raw"] = str(rp)

    if dry:
        res["ok"] = True
        res["detail"] = ("dry: would ingest "
                         f"{len(chunks)} chunks (conservation {ratio:.3f})")
        return res

    db = str(db or CHUNK_DB)
    Path(db).parent.mkdir(parents=True, exist_ok=True)   # fresh home (first ingest on a new install): create the store
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    _ensure_store(con)
    try:
        _delete_source(con, source)
        ids = [_chunk_id(source, i) for i in range(len(chunks))]
        rows = [(ids[i], chunks[i], source, jurisdiction, cite)
                for i in range(len(chunks))]
        con.executemany(
            "INSERT INTO chunk_text (chunk_id,text,source,jurisdiction,citations) "
            "VALUES (?,?,?,?,?)", rows)
        # BOTH tables — no sync trigger exists; a single-table write leaves FTS stale.
        con.executemany(
            "INSERT INTO chunk_text_fts (text, source_id) VALUES (?,?)",
            [(chunks[i], ids[i]) for i in range(len(chunks))])
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        res["detail"] = f"write failed: {type(e).__name__}: {str(e)[:140]}"
        return res
    finally:
        pass

    # verify: COUNT + FTS MATCH on the operative phrase. Never report success
    # without the probe hitting.
    n = con.execute("SELECT COUNT(*) FROM chunk_text WHERE source=?",
                    (source,)).fetchone()[0]
    ph = (phrase or "").strip().replace('"', " ")
    probe_hits = None
    if ph:
        try:
            probe_hits = con.execute(
                "SELECT COUNT(*) FROM chunk_text_fts WHERE chunk_text_fts MATCH ?",
                ('"' + ph + '"',)).fetchone()[0]
        except Exception:
            probe_hits = -1
    con.close()
    res["chunks"] = n
    res["probe_hits"] = probe_hits
    if n == 0:
        res["detail"] = "probe: 0 chunks on re-read (insert reported no error)"
        return res
    if ph and probe_hits in (None, 0, -1):
        res["detail"] = ("probe MISSED the operative phrase "
                         f"{phrase!r} — verify the phrase, not the ingest")
        return res
    res["ok"] = True
    res["detail"] = (f"ingested {n} chunks; probe_hits={probe_hits}; "
                     "semantic search after next embed delta "
                     "(`python3 -m pipeline.turbovec_ingest`)")
    _manifest(source, res, origin_url=origin_url, family=family,
              jurisdiction=jurisdiction, cite=cite, phrase=phrase)
    return res


def _manifest(source, res, origin_url="", family="", jurisdiction="", cite="", phrase=""):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    row = {"source": source, "ts": _now(), "chunks": res.get("chunks", 0),
           "probe_hits": res.get("probe_hits"), "conservation": res.get("conservation"),
           "raw": res.get("raw", ""), "origin": origin_url, "family": family,
           "jurisdiction": jurisdiction, "cite": cite, "probe_phrase": phrase,
           "ok": res.get("ok", False)}
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------- fetch
def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------- main
def _print_check(check):
    print(f"VERDICT: {check['verdict']}")
    if check.get("api_down"):
        print("  NOTE: semantic API was down for this pass — deterministic "
              "channels still decided; re-run the semantic tier when it is up.")
    if check.get("fts_error"):
        print(f"  NOTE: FTS probe errored: {check['fts_error']}")
    if check["present"]:
        print("  -> STOP. Cite the existing key, NO pull:")
        for c in cite_existing(check):
            print(f"     {c}")
        for fam, n in check["family_hits"].items():
            print(f"     family {fam}: {n} chunks")
    else:
        print("  -> TRUE all-families miss across: "
              f"{', '.join(check['checked_families'][:8])}{'...' if len(check['checked_families'])>8 else ''}")
        print("  -> may proceed to step 2 (official-publisher whitelist).")


def cmd_check(a):
    fams = a.family or []
    check = corpus_first_check(families=fams or None, phrase=a.phrase,
                               queries=(a.query or None))
    _print_check(check)
    return 0 if check["present"] else 1


def cmd_pull(a):
    source = a.source
    fams = (a.checked.split(",") if a.checked and a.checked.split(",")
            else None)
    # 1. check-first gate
    check = corpus_first_check(families=fams or ALL_FAMILY_PREFIXES[:12],
                               phrase=a.phrase, queries=(a.query or None))
    _print_check(check)
    if check["present"] and not a.force:
        print("REFUSED: already PRESENT in corpus — no pull (cite, don't re-ingest).")
        return 1

    # 2. publisher whitelist
    allowed, host, note = resolve_publisher(a.url, a.kind)
    print(f"PUBLISHER: allowed={allowed} host={host!r} :: {note}")
    if not allowed and not a.force:
        print("REFUSED: origin not on the official-publisher whitelist for this kind.")
        return 2

    # 3. force evidence
    ok, why = verify_force_evidence(a.force, fams, a.reason,
                                    check["checked_families"])
    if not ok:
        print(f"FORCE GATE FAILED: {why}")
        return 3
    if a.force:
        log_force({"source": source, "url": a.url, "kind": a.kind,
                   "checked_against": fams or [], "reason": a.reason,
                   "checked_families": check["checked_families"]})
        print(f"FORCE logged: checked_against={fams} reason={a.reason!r}")

    # 4. fetch (or local raw)
    if a.raw:
        text = Path(a.raw).read_text("utf-8")
        res_src = f"local-raw:{a.raw}"
    elif a.url:
        try:
            text = fetch(a.url)
            res_src = a.url
        except Exception as e:
            print(f"FETCH FAILED: {type(e).__name__}: {str(e)[:140]}")
            return 4
    else:
        print("REFUSED: pull needs --url or --raw <local file>.")
        return 4
    if not text.strip():
        print("FETCH EMPTY: body had no text (HTML? PDF? — save raw and pass --raw).")
        return 4

    # 5. ingest
    res = ingest_source(source, text, jurisdiction=a.jurisdiction, cite=a.cite,
                        phrase=a.phrase, family=a.family,
                        origin_url=a.url or res_src,
                        raw_path=a.raw, dry=a.dry)
    mark = "OK  " if res["ok"] else "--  "
    print(f"{mark}{res['detail']}")
    if res.get("raw"):
        print(f"     raw -> {res['raw']}")
    if res["ok"]:
        print(f"     CLOSE THE LOOP: update the flagging doc from the hedge to "
              f"[corpus:{source}].")
    return 0 if res["ok"] else 5


def cmd_state(a):
    if not STATE.exists():
        print("no force log yet (STATE not written)")
        return 0
    rows = json.loads(STATE.read_text("utf-8")) if STATE.exists() else []
    print(f"{len(rows)} force entr(y/ies) at {STATE}")
    for r in rows:
        print(f"  [{r.get('ts')}] {r.get('source')}: "
              f"checked={r.get('checked_against')} reason={r.get('reason')!r}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="corpus-first, official-publisher ingest gate (card t_562a9556)")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="step-1 gate: sweep all families, report PRESENT/MISSING")
    c.add_argument("--family", action="append",
                   help="family prefix to sweep (repeatable). Default: ALL families.")
    c.add_argument("--phrase", help="distinctive operative phrase (FTS arbiter)")
    c.add_argument("--query", action="append",
                   help="semantic query (repeatable, 2-3 differently-phrased)")
    c.set_defaults(fn=cmd_check)

    p = sub.add_parser("pull", help="check -> whitelist -> fetch -> ingest -> probe")
    p.add_argument("--source", required=True, help="source key to write")
    p.add_argument("--url")
    p.add_argument("--raw", help="local raw file (skip fetch)")
    p.add_argument("--kind", default="agency_manual",
                   help=f"authority kind: {sorted(OFFICIAL_PUBLISHERS)}")
    p.add_argument("--family", default="", help="raw-dir family")
    p.add_argument("--jurisdiction", default="US")
    p.add_argument("--cite", default="")
    p.add_argument("--phrase", help="operative-phrase probe (required to verify)")
    p.add_argument("--query", action="append")
    p.add_argument("--force", action="store_true")
    p.add_argument("--checked-against", dest="checked",
                   help="comma-separated families actually swept (required with --force)")
    p.add_argument("--reason", help="why the sweep missed / why pull anyway (required with --force)")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(fn=cmd_pull)

    s = sub.add_parser("state", help="show the force log")
    s.set_defaults(fn=cmd_state)

    a = ap.parse_args(argv)
    if getattr(a, "fn", None) is None:
        # default action: check (the workhorse) — mirror the check flags
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
