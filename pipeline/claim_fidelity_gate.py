#!/usr/bin/env python3
"""claim_fidelity_gate.py -- run the stands-for check AT AUDIT TIME, on m0's analysis.

USER-CORRECTED 2026-09-27: "claim_support_check it is an llm call shouldn't the check be
during the m0?" Correct, and it matches the locked doctrine already proven by
authority_bank_gate: gates live UPSTREAM in analysis, so a defect is fixed at SOURCE and
therefore fixed for every future filing. Checking at the m1 tail only discovers the defect
after a drafter already consumed it.

The defect class this closes -- the reason `authority_bank_gate` alone is not enough:
that gate asks "does this cite EXIST and is it verifiable" (cite-existence). It cannot ask
"does the authority say what the analysis uses it FOR" (proposition fidelity). Progressive
cited Hart v. Arnold, 884 A.2d 316, at pin 339 for gist-of-the-action; the analysis answered
by distinguishing Hart's dam-construction FACTS. Every cite in that exchange is real and
verifiable, so the cite gate passes it clean -- while the answer misses the proposition the
case was cited for. That is precisely WRONG_POINT, and claim_support_check computes it.

Verdicts stamped on the record (claim_support_check's own vocabulary):
  SUPPORTED   -- the corpus text backs the proposition
  OVERSTATED  -- the authority provides materially less than claimed
  WRONG_POINT -- the authority is about a different issue than the proposition
  UNBACKED    -- no support found / authority not in corpus
  UNVERIFIED  -- no lane was reachable (fail-safe; never rubber-stamps)

OVERSTATED / WRONG_POINT / UNBACKED are demoted into `claim_fidelity_rejected` on the
record, mirroring authority_bank_gate's `authority_rejected` contract, so no drafter can
weave a mischaracterized authority into a filing.

Fail-safe by construction: any error, any unreachable lane, yields UNVERIFIED and NEVER
edits or deletes analysis content. A gate that silently drops a point is worse than the
defect it was built to catch.

CLI
    python3 -m pipeline.claim_fidelity_gate <case_slug> [--dry-run] [--limit N]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import datetime
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

# BASE from the module's own location breaks whenever the module is exercised from a copy
# (sandbox run, relocated checkout): it then points at the copy's empty output/audit and every
# artifact reads as absent -- indistinguishable from "this case was never analyzed". Same
# failure the style-profile loader had. Env override first, real tree as the floor.
BASE = Path(os.environ.get("BEEZLE_BASE") or Path(__file__).resolve().parent.parent)
AUDIT_DIR = Path(os.environ.get("BEEZLE_AUDIT_DIR") or (BASE / "output" / "audit"))
CACHE = AUDIT_DIR / "claim_fidelity_cache.json"
CASES = Path(os.environ.get("BEEZLE_CASES_DIR") or (BASE / "obsidian_sync" / "cases"))

log = logging.getLogger("claim_fidelity_gate")

# Fields on a violation record that can carry an argued proposition + its authority.
_TEXT_FIELDS = ("description", "text", "quote", "analysis", "argument", "holding",
                "controlling_authority", "authority", "rule")

_DEMOTE = {"OVERSTATED", "WRONG_POINT", "UNBACKED"}


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _audit_path(case_id: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id.split(":", 1)[-1])
    cands = sorted(AUDIT_DIR.glob(f"data_*{safe}*.json"))
    return cands[0] if cands else None


def _case_facts(case_id: str) -> str:
    """m0's own analysis prose -- where a mischaracterized authority actually lives."""
    p = CASES / case_id / "CASE_FACTS.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        return ""


def _extract(md: str, limit: int = 0):
    """(proposition, authority, kind) triples via claim_support_check's own extractor."""
    try:
        from pipeline.claim_support_check import extract_claims
    except Exception as e:
        log.warning(f"extract_claims unavailable: {e}")
        return []
    try:
        claims = extract_claims(md) or []
    except Exception as e:
        log.warning(f"extract_claims failed: {e}")
        return []
    return claims[:limit] if limit else claims


_REPORTER = re.compile(
    r"\b(\d{1,3})\s+(U\.?\s?S\.?|S\.?\s?Ct\.?|A\.(?:2d|3d)|F\.(?:2d|3d)|"
    r"F\.?\s?Supp\.?(?:\s?[23]d)?|Pa\.(?:\s?Super\.?|\s?Cmwlth\.?)?)\s+(\d{1,4})\b")


def _cite_scoped_retriever():
    """retr(query, k) that FIRST pins the CITED OPINION'S OWN TEXT by source key.

    Why this exists (measured, not theoretical): the stock semantic retriever answered a
    D'Ambrosio, 431 A.2d 966 query with ERISA section 431, a Gregg 245 A.3d 637 query with tax
    code, and a 760 F.Supp.2d 520 query with "disability services and fish hatcheries". The
    grader then correctly reported no support -- producing 19 UNBACKED verdicts on authorities
    the corpus actually HOLDS. That is the false-absence class: a retrieval defect wearing the
    costume of a corpus gap, and if those verdicts had been trusted they would have demoted 19
    good authorities out of the filing.

    The cite is a RIGID identifier (vol + reporter + page), so resolve it structurally instead
    of hoping a nearest-neighbour lands on the right opinion:
      1. FTS-match the quoted cite -> candidate chunks -> their `source` keys;
      2. keep keys whose OWN text is the opinion (a key containing the cite in its head matter
         or a key naming the reporter triple), and pull that source's chunks directly;
      3. fall back to the stock semantic retriever only when no cite can be parsed.

    Step 2 matters because an FTS hit on a cite is usually a case CITING it. Reading subject
    matter off a citing case is the exact error that produced the false "m0 hallucinated Hart"
    claim earlier in this session -- so prefer a source key built FROM the cite, and only then
    a citing chunk.
    """
    try:
        from pipeline.claim_support_check import _get_retriever
        stock = _get_retriever()
    except Exception:
        stock = lambda q, k=10: []

    db_path = (_eroot() / "corpus/chunk_text.db")

    def _by_cite(cite: str, k: int):
        m = _REPORTER.search(cite or "")
        if not m or not db_path.is_file():
            return []
        vol, rep, page = m.group(1), m.group(2), m.group(3)
        norm = re.sub(r"[^a-z0-9]", "", rep.lower())          # a2d, us, fsupp2d
        key_frag = f"{vol}{norm}{page}"                        # 884a2d316, 600us122
        out = []
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = db.cursor()
            # (a) a source KEY built from this cite = the opinion itself, highest confidence.
            # ANCHOR the fragment at a key boundary: a bare %600us122% also matched
            # `606us1r52_3eah` (a volume-scan key), handing back an unrelated SCOTUS volume and
            # reading as "Mallory not in corpus". Case keys end with the cite fragment
            # (`..._884a2d316`), so require the underscore-delimited tail.
            cur.execute("SELECT source, text FROM chunk_text "
                        "WHERE source LIKE ? OR source LIKE ? LIMIT ?",
                        (f"%_{key_frag}", f"%_{key_frag}_%", k))
            out += [{"source": r[0], "text": " ".join((r[1] or "").split())}
                    for r in cur.fetchall()]
            db.close()
        except Exception:
            return out
        return out[:k]

    def _by_cite_fts(cite: str, k: int):
        """LAST RESORT: FTS the cite and return whatever chunks mention it.

        These are usually cases CITING the target, so this lane runs ONLY after the
        key-anchored and party-name lanes fail -- reading a cite's subject matter off a citing
        case is precisely the error that produced the false "Hart is not a dam case" claim.
        Ordering matters more than presence here: when this lane ran first (because it returns
        SOMETHING for nearly any cite), it short-circuited the authoritative lanes and every
        parallel-cited case read as unsupported.
        """
        m = _REPORTER.search(cite or "")
        if not m or not db_path.is_file():
            return []
        vol, rep, page = m.group(1), m.group(2), m.group(3)
        out = []
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = db.cursor()
            ids = []
            for q in {f'"{vol} {rep} {page}"', f'"{vol} {rep.replace(". ", ".")} {page}"'}:
                try:
                    cur.execute("SELECT source_id FROM chunk_text_fts "
                                "WHERE chunk_text_fts MATCH ? LIMIT ?", (q, k))
                    ids += [r[0] for r in cur.fetchall()]
                except sqlite3.Error:
                    continue
            for cid in dict.fromkeys(ids):
                cur.execute("SELECT source, text FROM chunk_text WHERE chunk_id=?", (cid,))
                r = cur.fetchone()
                if r:
                    out.append({"source": r[0], "text": " ".join((r[1] or "").split())})
            db.close()
        except Exception:
            return out
        return out[:k]

    def _by_party(query: str, k: int):
        """PARALLEL-CITE fallback: resolve by LEAD PARTY surname in the source key.

        A case is often keyed under its OTHER reporter: Mallory sits at
        `scotus_mallory_v_norfolk_southern_2023` (no `600us122` anywhere) and Cowden at
        `scotus_cowden_v_aetna_casualty_and_surety_co_389pa459` (official Pa. cite, not the
        A.2d one pleaded). A cite-fragment-only lookup declares both absent -- a false gap on
        law the corpus holds, which is exactly what the skill's parallel-cite rule warns about.

        Surname + `_v_` in the KEY is the anchor, never body text: source keys are written at
        ingest from the document's own caption, so they are provenance; a party name appearing
        in body text usually means a case CITING the target.
        """
        m = re.search(r"([A-Z][A-Za-z'\u2019-]{2,})\s+v\.?\s+", str(query or ""))
        if not m or not db_path.is_file():
            return []
        lead = re.sub(r"[^a-z]", "", m.group(1).lower())
        if len(lead) < 3:
            return []
        out = []
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = db.cursor()
            cur.execute("SELECT source, text FROM chunk_text "
                        "WHERE source LIKE ? OR source LIKE ? LIMIT ?",
                        (f"%_{lead}_v_%", f"{lead}_v_%", k))
            out = [{"source": r[0], "text": " ".join((r[1] or "").split())}
                   for r in cur.fetchall()]
            db.close()
        except Exception:
            return out
        return out[:k]

    def retr(query, k: int = 10):
        hits = []
        for m in _REPORTER.finditer(str(query or "")):
            hits += _by_cite(m.group(0), k)
            if hits:
                break
        if not hits:
            hits = _by_party(query, k)          # parallel-cite / other-reporter key
        if not hits:                            # citing chunks: weakest evidence, last
            for m in _REPORTER.finditer(str(query or "")):
                hits += _by_cite_fts(m.group(0), k)
                if hits:
                    break
        if hits:
            return hits[:k]
        try:
            return stock(query, k=k)
        except Exception:
            return []

    return retr


def _grade(claims):
    """Grade with claim_support_check's parallel grader, cite-scoped retrieval. Never raises."""
    if not claims:
        return []
    try:
        from pipeline.claim_support_check import _grade_all
        return _grade_all(claims, _cite_scoped_retriever())
    except Exception as e:
        log.warning(f"grading unavailable: {e}")
        return [{"verdict": "UNVERIFIED", "quote": "",
                 "why": f"grader unavailable: {e}"[:60]} for _ in claims]


def _norm_cite(auth: str) -> str:
    """SPINE/bank/movant write the same cite with and without a parenthetical -- key both."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", (auth or "").strip()).strip()


def _bank_path(case_id: str):
    """The rebuttal bank IS the analysis artifact for a case with no data_*.json audit.

    Not every matter has an m0 `output/audit/data_<case>.json` -- a rebuttal-lane case is
    analyzed into `<slug>_rebuttal_bank.json` (entries: pattern/tier/heading/answer_heading/
    text/cites) and that bank is what the drafter actually consumes. Gating only the audit
    file would leave this case -- the live one -- entirely ungated, which is the "gate a write
    site instead of the chokepoint" mistake in a new costume.
    """
    p = AUDIT_DIR / f"{case_id}_rebuttal_bank.json"
    return p if p.is_file() else None


def gate_bank(case_id: str, write: bool = True, limit: int = 0) -> dict:
    """Grade every (proposition, authority) pair carried by bank entries; stamp each entry.

    A demoted verdict lands on the ENTRY the drafter reads, so an authority used for a
    proposition it does not carry cannot be woven into a filing.
    """
    path = _bank_path(case_id)
    if not path:
        return {}
    bank = json.loads(path.read_text(encoding="utf-8"))
    cache = _load_cache()
    counts = {"checked": 0, "SUPPORTED": 0, "OVERSTATED": 0, "WRONG_POINT": 0,
              "UNBACKED": 0, "UNVERIFIED": 0, "demoted": 0, "entries_stamped": 0}

    pairs, index = [], []
    for i, e in enumerate(bank):
        if not isinstance(e, dict):
            continue
        body = " ".join(str(e.get(f, "")) for f in ("text", "heading", "answer_heading"))
        body = " ".join(body.split())
        if not body:
            continue
        for cite in (e.get("cites") or []):
            cite = str(cite).strip()
            if not cite:
                continue
            pairs.append((body[:600], cite, "case"))
            index.append(i)
    if limit:
        pairs, index = pairs[:limit], index[:limit]

    fresh, fresh_ix, verdicts = [], [], {}
    for n, (prop, auth, kind) in enumerate(pairs):
        key = f"{_norm_cite(auth)}||{prop[:160]}"
        if key in cache:
            verdicts[n] = cache[key]
        else:
            fresh.append((prop, auth, kind))
            fresh_ix.append(n)
    graded = _grade(fresh)
    for n, (prop, auth, _k), r in zip(fresh_ix, fresh, graded):
        key = f"{_norm_cite(auth)}||{prop[:160]}"
        cache[key] = {"verdict": r.get("verdict", "UNVERIFIED"),
                      "quote": (r.get("quote") or "")[:300],
                      "why": (r.get("why") or "")[:160]}
        verdicts[n] = cache[key]

    touched = set()
    for n, (prop, auth, _k) in enumerate(pairs):
        v = verdicts.get(n)
        if not v:
            continue
        counts["checked"] += 1
        counts[v["verdict"] if v["verdict"] in counts else "UNVERIFIED"] += 1
        e = bank[index[n]]
        e.setdefault("claim_fidelity", {})[_norm_cite(auth)] = v["verdict"]
        touched.add(index[n])
        if v["verdict"] in _DEMOTE:
            e.setdefault("claim_fidelity_rejected", []).append(
                {"authority": _norm_cite(auth), "verdict": v["verdict"], "why": v["why"]})
            counts["demoted"] += 1
    counts["entries_stamped"] = len(touched)

    if write:
        path.write_text(json.dumps(bank, indent=1, ensure_ascii=False), encoding="utf-8")
    _save_cache(cache)
    log.info(f"claim_fidelity_gate[bank] {case_id}: {counts}")
    return counts


def gate_case(case_id: str, write: bool = True, limit: int = 0) -> dict:
    """Grade every authority-bearing proposition in m0's analysis; stamp the record.

    Mirrors authority_bank_gate.gate_case: counts dict returned, one organ record pinned
    into procedural_violations (results stream, never a side key), cache reused across runs.
    """
    path = _audit_path(case_id)
    if not path:
        log.warning(f"no audit file for {case_id}")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    cache = _load_cache()
    counts = {"checked": 0, "SUPPORTED": 0, "OVERSTATED": 0, "WRONG_POINT": 0,
              "UNBACKED": 0, "UNVERIFIED": 0, "demoted": 0, "records_stamped": 0}

    md = _case_facts(case_id)
    if not md:
        log.warning(f"no CASE_FACTS.md for {case_id}; nothing to grade")
        return counts

    claims = _extract(md, limit=limit)
    fresh, cached = [], {}
    for prop, auth, kind in claims:
        key = f"{_norm_cite(auth)}||{(prop or '')[:160]}"
        if key in cache:
            cached[key] = cache[key]
        else:
            fresh.append((prop, auth, kind))
    graded = _grade(fresh)
    for (prop, auth, kind), r in zip(fresh, graded):
        key = f"{_norm_cite(auth)}||{(prop or '')[:160]}"
        cache[key] = {"verdict": r.get("verdict", "UNVERIFIED"),
                      "quote": (r.get("quote") or "")[:300],
                      "why": (r.get("why") or "")[:160]}
        cached[key] = cache[key]

    # Index verdicts by normalized cite so a record can be stamped for the authority it names
    by_cite: dict = {}
    for prop, auth, _kind in claims:
        key = f"{_norm_cite(auth)}||{(prop or '')[:160]}"
        v = cached.get(key)
        if not v:
            continue
        counts["checked"] += 1
        counts[v["verdict"] if v["verdict"] in counts else "UNVERIFIED"] += 1
        by_cite.setdefault(_norm_cite(auth), []).append(
            {"proposition": (prop or "")[:240], **v})

    # Stamp the violation stream: a record naming an authority carries that authority's
    # fidelity verdict, and a demoted verdict is listed so no drafter may weave it.
    for stream in ("procedural_violations", "violations", "jurisdictional_defects"):
        for rec in data.get(stream, []) or []:
            if not isinstance(rec, dict):
                continue
            blob = " ".join(str(rec.get(f, "")) for f in _TEXT_FIELDS)
            if not blob.strip():
                continue
            stamped = False
            for cite, verdicts in by_cite.items():
                if not cite or cite not in blob:
                    continue
                worst = None
                for v in verdicts:
                    if v["verdict"] in _DEMOTE:
                        worst = v if (worst is None or v["verdict"] == "WRONG_POINT") else worst
                slot = rec.setdefault("claim_fidelity", {})
                slot[cite] = sorted({v["verdict"] for v in verdicts})
                stamped = True
                if worst is not None:
                    rec.setdefault("claim_fidelity_rejected", []).append({
                        "authority": cite, "verdict": worst["verdict"],
                        "why": worst["why"], "proposition": worst["proposition"]})
                    counts["demoted"] += 1
            if stamped:
                counts["records_stamped"] += 1

    organ = {
        "category": "CLAIM_FIDELITY_GATE", "severity": "META",
        "file": "analysis", "doc_category": "organ",
        "description": (
            f"analysis-side proposition-fidelity gate: {counts['checked']} claim(s) graded "
            f"against the corpus — {counts['SUPPORTED']} SUPPORTED, "
            f"{counts['OVERSTATED']} OVERSTATED, {counts['WRONG_POINT']} WRONG_POINT, "
            f"{counts['UNBACKED']} UNBACKED, {counts['UNVERIFIED']} UNVERIFIED; "
            f"{counts['demoted']} authority-use(s) demoted so no drafter may cite them for "
            f"a proposition the authority does not carry"),
        "gate_counts": counts,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    pv = data.setdefault("procedural_violations", [])
    pv[:] = [r for r in pv if r.get("category") != "CLAIM_FIDELITY_GATE"] + [organ]

    if write:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _save_cache(cache)
    log.info(f"claim_fidelity_gate {case_id}: {counts}")
    return counts


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="grade only the first N claims (smoke-test)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    c = gate_case(a.case_id, write=not a.dry_run, limit=a.limit)
    if not c:
        # No data_*.json for this matter -> gate the artifact that DOES exist.
        c = gate_bank(a.case_id, write=not a.dry_run, limit=a.limit)
        if c:
            print("[lane] rebuttal bank (no data_*.json audit for this case)")
    print(json.dumps(c, indent=1) if c else "no analysis artifact found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
