#!/usr/bin/env python3
"""corpus_gap_bot — NOT_IN_CORPUS is a work order, not a verdict.

The user's anti-hedge rule: never caveat coverage. Either verify presence across all
families, or PULL and INGEST, then state the verified location.

This closes the loop between two pieces that already existed and never spoke:

  pipeline/cite_verifier.py   finds cites and classifies them
                              VERIFIED / WRONG_CITE / NOT_IN_CORPUS
  pipeline/case_fetch.py      fetch_opinion()  CAP static bulk -> parallel cite ->
                              Wikisource -> CourtListener REST v4 (authenticated)
                              ingest_case()    writes chunk_text AND chunk_text_fts

Nothing ran them together, so a gap stayed a gap and the next draft cited it anyway.

Flow per gap:
    1. re-probe (3-tier: the family may hold it under another reporter)
    2. fetch_opinion  -- CAP first, keyless, no rate limit
    3. NAME-GUARD     -- a cite lookup can return a case that merely CITES the target
                         (live failure: "Drake v. Rogers, 32 Me. 524" pulled Almich v.
                         Downey). Mismatch = REJECT. A mismatched ingest is corpus
                         poisoning.
    4. ingest_case    -- idempotent (DELETE by source, then INSERT)
    5. probe          -- verify with a distinctive passage; never report success on the
                         strength of an insert returning no error
    6. re-verify      -- run the cite back through the gate; it must now read VERIFIED

Semantic search sees new chunks only after the next embed delta
(`python3 -m pipeline.turbovec_ingest`). Deterministic sqlite/FTS reaches them at once.
This bot says so in its report rather than implying full searchability.

Usage:
    python3 -m pipeline.corpus_gap_bot --file <path.md>      # scan, fill gaps
    python3 -m pipeline.corpus_gap_bot --file <p> --dry      # report only
    python3 -m pipeline.corpus_gap_bot --case "X v. Y" --cite "170 A.3d 364"
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, os, re, json, time, argparse, sqlite3, datetime
from pathlib import Path

sys.path.insert(0, str(_epkg()))

from pipeline.cite_verifier import (
    scan_file, scan_text, verify_case_cite, _find_legaldb, _party_key,
)

try:
    from pipeline.case_fetch import fetch_opinion, ingest_case, parse_cite, CHUNK_DB
    _FETCH = True
except Exception as e:  # pragma: no cover
    _FETCH = False
    _FETCH_ERR = str(e)
    CHUNK_DB = (_eroot() / "corpus/chunk_text.db")

LOG = (_eroot() / "output/corpus_gap_bot.jsonl")
LEARN = (_eroot() / "output/learnings.jsonl")


# ---------------------------------------------------------------- name guard
_STOP = {"v", "vs", "inc", "co", "corp", "llc", "llp", "ltd", "the", "of", "and",
         "company", "insurance", "ins", "et", "al", "national", "natl"}


def _tokens(name):
    return {w for w in re.split(r"[^A-Za-z]+", (name or "").lower())
            if len(w) > 2 and w not in _STOP}


def _lead_party(name):
    """The FIRST party's distinctive surname -- the only token that proves identity.

    Everything before 'v.' is the appellant/plaintiff. Its surname is what makes
    a case THAT case; reporter/industry words around it are shared by thousands.
    """
    head = re.split(r"\bv[s]?\.?\b", (name or ""), maxsplit=1)[0]
    toks = [w for w in re.split(r"[^A-Za-z]+", head.lower())
            if len(w) > 2 and w not in _STOP]
    return toks[-1] if toks else None


def name_guard(requested, fetched_text, fetched_name=""):
    """True only if the fetched document is plausibly the REQUESTED case.

    A cite lookup can return a case that merely cites the target, or -- worse --
    an unrelated case reached through a bogus parallel-cite resolution.

    ANY-TOKEN MATCHING IS NOT A GUARD. It let Rancosky's opinion be ingested as
    Cowden: "Cowden v. Aetna Casualty and Surety Co." tokenizes to
    {cowden, aetna, casualty, surety}, and Rancosky's head matter contains
    "casualty" (Washington National Casualty), so the guard returned
    True/"matched ['casualty']" and 53KB of the WRONG opinion was keyed as Cowden
    in both chunk_text and chunk_text_fts. Generic industry words
    (casualty/surety/mutual/fire/life/bank/...) are shared by thousands of
    reporter captions and prove nothing.

    The LEAD PARTY'S SURNAME must appear in the head matter. That is the token
    that makes a case that case.
    """
    want = _tokens(requested)
    if not want:
        return False, "no distinctive token in requested name"
    head = ((fetched_name or "") + " " + (fetched_text or "")[:1200]).lower()

    lead = _lead_party(requested)
    if lead and lead not in head:
        return False, (f"lead party {lead!r} absent from fetched head matter "
                       f"(refusing: a generic-token match is not identity)")

    # The lead party must appear in the CAPTION, not merely somewhere in the window.
    # A CITING case quotes the target by name: Halpern v. Ricoh quotes "Gregg v.
    # Ameriprise Financial, 245 A.3d 637, 646" in its body, so a plain head-window
    # test passes and 37KB of Halpern gets keyed as Gregg (live failure). The
    # caption is the text BEFORE the first court/argued/decided marker; a party
    # named only after it is being cited, not decided.
    if lead:
        caption = re.split(
            r"\b(?:supreme court|superior court|court of appeals|district court|"
            r"commonwealth court|argued|decided|submitted|filed|opinion by|"
            r"before\s|no\.\s*\d|j-a?\d|\d{4}\s+pa\s+super)\b",
            head, maxsplit=1)[0]
        if lead not in caption:
            return False, (f"lead party {lead!r} appears only AFTER the caption "
                           f"(fetched document CITES the target, it is not the target)")

    hit = {w for w in want if w in head}
    if not hit:
        return False, f"none of {sorted(want)[:4]} in fetched head matter"
    return True, f"matched {sorted(hit)[:4]} incl. lead party {lead!r}"


# ---------------------------------------------------------------- probe
def probe_ingested(source_key, phrase=None, db=None):
    """Verify by COUNT + distinctive-passage probe. Never trust a silent insert."""
    dbp = str(db or CHUNK_DB)
    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM chunk_text WHERE source=?",
                        (source_key,)).fetchone()[0]
        hit = None
        if phrase and n:
            words = [w for w in re.split(r"\W+", phrase) if len(w) > 4][:4]
            if words:
                like = "%" + "%".join(words) + "%"
                hit = con.execute(
                    "SELECT COUNT(*) FROM chunk_text WHERE source=? AND text LIKE ?",
                    (source_key, like)).fetchone()[0]
        return {"chunks": n, "phrase_hits": hit}
    finally:
        con.close()


# ---------------------------------------------------------------- one gap
def fill_gap(case, cite, db=None, dry=False):
    """Pull one missing authority and prove it landed."""
    out = {"case": case, "cite": cite, "action": None, "ok": False,
           "detail": "", "source_key": None, "chunks": 0,
           "ts": datetime.datetime.now().isoformat(timespec="seconds")}

    legaldb = db or _find_legaldb()()

    # 1. re-probe before spending a fetch: the family may hold it elsewhere
    m = re.match(r"\s*(\d{1,3})\s+(.+?)\s+(\d{1,4})\s*$", cite or "")
    if m:
        pre = verify_case_cite(case, m.group(1), m.group(2), m.group(3), db=legaldb)
        if pre.get("status") == "VERIFIED":
            out.update(action="already_present", ok=True,
                       detail=f"re-probe VERIFIED at {pre.get('found_cite') or cite}")
            return out
        if pre.get("status") == "WRONG_CITE":
            out.update(action="wrong_cite_not_gap", ok=True,
                       detail=("corpus holds this case at "
                               f"{pre.get('found_cite')} (src {pre.get('source')}) "
                               "-- correct the citation, do not pull"))
            return out

    if dry:
        out.update(action="would_fetch", detail="dry run")
        return out

    if not _FETCH:
        out.update(action="fetch_unavailable", detail=_FETCH_ERR[:160])
        return out

    # 2. fetch: CAP -> parallel cite -> Wikisource -> CourtListener
    #    Real API: fetch_opinion returns a TUPLE (text, resolved_name, provider).
    try:
        text, resolved, route = fetch_opinion(case, cite)
    except Exception as e:
        out.update(action="fetch_error", detail=f"{type(e).__name__}: {str(e)[:140]}")
        return out

    if not text:
        out.update(action="fetch_miss",
                   detail="all routes returned nothing (CAP/parallel/Wikisource/CL)")
        return out

    route = route or "?"

    # 3. NAME GUARD -- reject a citing case masquerading as the target.
    #    Must run BEFORE ingest_case(), which re-fetches and writes in one step: there
    #    is no hook between its fetch and its INSERT, so the guard decides here whether
    #    ingest_case is allowed to be called at all.
    ok, why = name_guard(case, text, resolved or "")
    if not ok:
        out.update(action="rejected_name_guard",
                   detail=f"route={route}; {why}; NOT ingested (poisoning risk)")
        return out

    # 4. ingest (idempotent: skips when source already present; writes BOTH
    #    chunk_text and chunk_text_fts, since no sync trigger exists)
    try:
        res = ingest_case(case, cite)
    except Exception as e:
        out.update(action="ingest_error", detail=f"{type(e).__name__}: {str(e)[:140]}")
        return out

    key = (res or {}).get("source")
    out["source_key"] = key
    status = (res or {}).get("status")
    if status in ("miss", "empty"):
        out.update(action=f"ingest_{status}",
                   detail=f"route={route}; ingest_case reported {status}")
        return out

    # 5. probe -- a distinctive span from the fetched text
    span = ""
    for para in re.split(r"\n\s*\n", text):
        s = para.strip()
        if 60 < len(s) < 400:
            span = s
            break
    pr = probe_ingested(key, span, db=None) if key else {"chunks": 0}
    out["chunks"] = pr.get("chunks", 0)

    if not out["chunks"]:
        out.update(action="ingest_unverified",
                   detail=f"route={route}; insert reported no error but 0 chunks on probe")
        return out

    # 5b. G6' (PLAN_obsidian_corpus_graph §14): struct index + _graph hub for the new slug in the same lane.
    #     Flag-gated (GRAPH_EMIT=1) so a failing index/vault sync can never fail an ingest; deterministic, idempotent
    #     (shape = official_source_puller). Skipped when ingest_case already emitted (its res carries graph_emit).
    if os.environ.get("GRAPH_EMIT") == "1" and "graph_emit" not in (res or {}):
        try:
            from pipeline.struct_index_build import index_slug
            out["struct_index"] = index_slug(key)
        except Exception as e:  # noqa: BLE001 — struct index is a side effect of ingest, never its gate
            out["struct_index"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            from pipeline.corpus_graph_build import graph_emit
            out["graph_emit"] = graph_emit([key])
        except Exception as e:  # noqa: BLE001 — graph is a side effect of ingest, never its gate
            out["graph_emit"] = {"error": f"{type(e).__name__}: {e}"}
    elif "graph_emit" in (res or {}):
        out["graph_emit"] = res["graph_emit"]; out["struct_index"] = res.get("struct_index")

    # 6. re-verify through the gate
    post = "n/a"
    if m:
        pv = verify_case_cite(case, m.group(1), m.group(2), m.group(3),
                              db=_find_legaldb()())
        post = pv.get("status")

    out.update(action="ingested", ok=True,
               detail=(f"route={route}; {out['chunks']} chunks; "
                       f"probe_hits={pr.get('phrase_hits')}; gate={post}; "
                       "semantic search after next embed delta"))
    return out


# ---------------------------------------------------------------- batch
def run_file(path, dry=False, limit=25):
    legaldb = _find_legaldb()()
    scan = scan_file(path, db=legaldb)
    report = {"path": str(path), "total_cites": scan.get("total", 0),
              "wrong": scan.get("wrong", []), "filled": [], "gaps": scan.get("gaps", [])}

    for g in scan.get("gaps", [])[:limit]:
        r = fill_gap(g.get("case", ""), g.get("cite", ""), db=legaldb, dry=dry)
        report["filled"].append(r)
        if not dry:
            time.sleep(2.5)  # CourtListener rate limit
    return report


def _log(rows):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--case")
    ap.add_argument("--cite")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()

    if a.case and a.cite:
        r = fill_gap(a.case, a.cite, dry=a.dry)
        print(json.dumps(r, indent=2))
        _log([r])
        return 0 if r["ok"] else 1

    if a.file:
        rep = run_file(a.file, dry=a.dry, limit=a.limit)
        print(f"{rep['path']}: {rep['total_cites']} cites, "
              f"{len(rep['wrong'])} WRONG_CITE, {len(rep['gaps'])} gaps")
        for w in rep["wrong"]:
            print(f"  WRONG_CITE  {w['case'][:44]}: {w['cite']} -> {w['found_cite']}")
        for r in rep["filled"]:
            mark = "OK " if r["ok"] else "-- "
            print(f"  {mark}{r['action']:22s} {r['case'][:36]} {r['cite']}")
            print(f"       {r['detail']}")
        _log(rep["filled"])
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
