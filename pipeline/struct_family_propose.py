#!/usr/bin/env python3
"""struct_family_propose.py — bounded LLM COMPREHENSION pass over the struct index catch-all (SANDBOX, 2026-11-10).

Why: struct_index_build.py classifies a chunk's family from its source slug by a prefix table (FAMILIES). 13,711 distinct
sources (353,910 chunks) land in the catch-all `unclassified`. Adding 13,711 hand patterns would be a keyword table that
fakes comprehension (rejected by doctrine). Instead: deterministic code gathers EVIDENCE per source (slug + row count +
jurisdiction + first chunks, read-only from chunk_text.db), the LLM does the comprehension (which EXISTING family, or a NEW
one), and deterministic code parses strictly, checkpoints, and later applies only high-confidence proposals (see
struct_family_apply.py). Zero writes to the struct DB here; this script only writes under output/research/.

Evidence per source : source key, n rows, jurisdictions seen, first 300 chars of the first 2 chunks (by ord) + 1 mid chunk.
Model               : local-gpu exl3 lane http://localhost:8000/v1 (qwen3.8-27b-exl3), thinking OFF, 4-6 concurrent requests.
Output              : output/research/struct_family_proposals.jsonl  (append-only; one line per source; model + ts)
                      output/research/struct_family_evidence.jsonl   (evidence cache so a re-run never re-reads chunk_text)
                      output/research/struct_family_proposals.md     (summary grouped by proposed family)
Resumable           : sources already present in the proposals jsonl are skipped; every batch is flushed on completion.

Run : cd ~/beezle/_sandbox_struct && env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES /usr/bin/python3 \
          pipeline/struct_family_propose.py [--batch 32] [--concurrency 5] [--minutes 80] [--limit N] [--summary-only]
Verify: /usr/bin/python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" pipeline/struct_family_propose.py
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, re, sqlite3, sys, threading, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from struct_index_build import FAMILIES, ORPHAN                                    # noqa: E402  (families are data)

SANDBOX = HERE.parent
STRUCT_DB = Path(os.environ.get("STRUCT_DB", str(SANDBOX / "struct_index.db")))
CHUNK_DB = Path(os.environ.get("CHUNK_DB", str(_eroot() / "corpus/chunk_text.db")))
OUT_DIR = Path(os.environ.get("STRUCT_RESEARCH_DIR", str(SANDBOX / "output" / "research")))
PROPOSALS = OUT_DIR / "struct_family_proposals.jsonl"
EVIDENCE = OUT_DIR / "struct_family_evidence.jsonl"
SUMMARY = OUT_DIR / "struct_family_proposals.md"

LLM_URL = os.environ.get("BEEZLE_B70_URL", "http://localhost:8000/v1/chat/completions")
MODEL = os.environ.get("BEEZLE_B70_MODEL", "qwen3.8-27b-exl3")
KINDS = ("statute", "regulation", "rule", "case", "treatise", "dictionary", "document", "guidance", "form", "unclassified")
# builder kinds -> proposal kinds (the builder says reg / local_rule)
KIND_MAP = {"reg": "regulation", "local_rule": "rule"}

# One-line meaning per EXISTING family, derived from the FAMILIES tuples' prefix + comment (vocabulary documentation for the
# prompt — this describes the families, it does not classify sources). Families absent here get an auto line from their prefix.
MEANING = {
    "pa_rcp": "Pa. Rules of Civil Procedure (231 Pa. Code)", "pa_rcp_hand": "Pa.R.C.P. hand-ingested rule files",
    "pa_rap": "Pa. Rules of Appellate Procedure (210 Pa. Code)", "pa_cs": "Pa. Consolidated Statutes by title",
    "pa_ps": "Purdon's Pa. Statutes (unconsolidated, P.S.)", "frcp": "Federal Rules of Civil Procedure",
    "frap": "Federal Rules of Appellate Procedure", "fre": "Federal Rules of Evidence",
    "usc_t15": "15 U.S.C. (commerce and trade)", "usc_t28": "28 U.S.C. (judiciary and judicial procedure)",
    "case_scotus_slug": "opinions keyed by case slug + cite (SCOTUS + PA + federal)", "case_cap_pa": "Caselaw Access Project PA opinions",
    "case_docvault": "opinions pulled into the docvault", "case_ca3": "Third Circuit opinions",
    "phila_cr": "Philadelphia criminal local rules", "phila_civ": "Philadelphia civil local rules",
    "cp_trial": "Court of Common Pleas trial division local rules", "edpa_lr": "E.D. Pa. local rules",
    "pa_code_other": "Pa. Code titles other than 210/231 (regulations)", "cfr_hand": "Code of Federal Regulations, hand-ingested by title",
    "courtlistener_other": "CourtListener opinions", "usc_rest": "U.S. Code titles other than 15/28",
    "pa_code_pacb": "Pa. Code / Pa. Bulletin NNN_NNNN keyed regulation files", "case_cap_us": "Caselaw Access Project U.S. (federal) opinions",
    "case_cap_other": "Caselaw Access Project other-state opinions", "case_nj_cap": "Caselaw Access Project New Jersey opinions",
    "case_usreports": "U.S. Reports bound volumes (SCOTUS, many opinions per source)", "cfr_govinfo": "CFR annual edition volumes from govinfo",
    "ecfr": "eCFR titles", "uscode_govinfo": "U.S. Code titles from govinfo / LII", "usc_title": "U.S. Code title dumps (USC-titleNN)",
    "courtlistener_plaw": "Public Laws via CourtListener", "courtlistener_cfr": "CFR parts via CourtListener",
    "courtlistener_rules": "court rules via CourtListener", "courtlistener_pa_code": "Pa. Code via CourtListener",
    "plaw": "Public Laws (PLAW-NNNpublNNN)", "comps": "govinfo compiled statutes (COMPS-NNNNN)", "njac": "N.J. Administrative Code",
    "nj_stat": "N.J. Statutes Annotated", "nj_court_rule": "N.J. Court Rules", "nj_other": "other New Jersey law (constitution etc.)",
    "pa_cons_stat": "Pa. Consolidated Statutes (pa_cons_stat_tNN_sNNNN keys)", "pa_consolidated": "one Pa. Consolidated Statutes dump",
    "treatise_amjur": "American Jurisprudence 2d", "treatise_cjs": "Corpus Juris Secundum", "treatise_encyc": "legal encyclopedias",
    "dictionary_blacks": "Black's Law Dictionary editions", "dictionary_bouvier": "Bouvier's Law Dictionary", "dictionary_other": "other law dictionaries",
    "case_usreports_pdfa": "U.S. Reports PDF/A volumes", "case_usreports_bv": "U.S. Reports bound volumes (NNNbv)",
    "case_usreports_pp": "U.S. Reports preliminary prints", "pa_bulletin": "Pennsylvania Bulletin issues (vol-issue)",
    "scotus_journal": "Journal of the Supreme Court of the United States", "trf": "thrift financial report dataset (non-law)",
    "irs": "IRS material (manual, notices, guidance)", "irs_pubs": "IRS publications (pNNN)", "frb": "Federal Reserve Board material",
    "fara": "Foreign Agents Registration Act material", "senate_manual": "Senate Manual", "sec_forms": "SEC forms",
    "ussgl": "U.S. Standard General Ledger (Treasury)", "numeric_doc": "numeric-keyed document dumps",
    "pa_other": "non-codified Pennsylvania material (guides, definitions)", "unclassified": "catch-all (do not propose)",
}


def vocabulary() -> list[tuple[str, str, str]]:
    out = []
    for name, prefix, kind, _trx in FAMILIES:
        if name == "unclassified": continue
        k = KIND_MAP.get(kind, kind)
        out.append((name, k, MEANING.get(name, f"sources keyed {prefix!r}")))
    return out


# ------------------------------------------------------------------------------------------------------------------------
# evidence (deterministic, read-only)
# ------------------------------------------------------------------------------------------------------------------------
def census(con_struct) -> list[tuple[str, int]]:
    return con_struct.execute("SELECT source, COUNT(*) FROM chunk_struct WHERE family='unclassified' AND source IS NOT NULL "
                              "GROUP BY source ORDER BY lower(source)").fetchall()


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists(): return []
    out = []
    with path.open() as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln: continue
            try: out.append(json.loads(ln))
            except json.JSONDecodeError: continue
    return out


def _clean(t: str, n: int = 300) -> str:
    return re.sub(r"\s+", " ", t or "").strip()[:n]


def gather_evidence(con_struct, con_chunk, source: str, n: int) -> dict:
    cids = [r[0] for r in con_struct.execute("SELECT chunk_id FROM chunk_struct WHERE source=? ORDER BY ord LIMIT 2", (source,))]
    if n > 4:
        mid = con_struct.execute("SELECT chunk_id FROM chunk_struct WHERE source=? ORDER BY ord LIMIT 1 OFFSET ?", (source, n // 2)).fetchone()
        if mid and mid[0] not in cids: cids.append(mid[0])
    heads = [r[0] for r in con_struct.execute("SELECT DISTINCT heading FROM chunk_struct WHERE source=? AND heading NOT IN ('', 'none') LIMIT 3", (source,))]
    texts, jurs, cites = [], Counter(), Counter()
    for c in cids:
        r = con_chunk.execute("SELECT substr(text,1,600), jurisdiction, substr(citations,1,200) FROM chunk_text WHERE chunk_id=?", (c,)).fetchone()
        if not r: continue
        texts.append(_clean(r[0], 300))
        if r[1]: jurs[r[1]] += 1
        for cit in (r[2] or "").split(";")[:3]:
            cit = cit.strip()
            if cit: cites[cit] += 1
    return {"source": source, "n": n, "jur": [j for j, _ in jurs.most_common(2)], "heads": heads, "texts": texts,
            "cites": [c for c, _ in cites.most_common(3)]}


# ------------------------------------------------------------------------------------------------------------------------
# prompt + strict parse
# ------------------------------------------------------------------------------------------------------------------------
SYSTEM = ("You classify SOURCES of a legal text corpus into families. A source is one ingested document or document set, "
          "identified by a slug key; you see the key, its chunk count, jurisdictions, and short text excerpts. "
          "Answer with ONE JSON array and nothing else — no prose, no markdown fence.")


def build_prompt(batch: list[dict], running_new: list[tuple[str, int]]) -> str:
    vocab = "\n".join(f"- {n} ({k}): {m}" for n, k, m in vocabulary())
    rn = ", ".join(f"{n} (x{c})" for n, c in running_new[:40]) or "(none yet)"
    items = []
    for e in batch:
        items.append(json.dumps({"source": e["source"], "chunks": e["n"], "jurisdiction": e["jur"], "headings": e["heads"],
                                 "citations": e["cites"], "excerpts": e["texts"]}, ensure_ascii=False))
    return (
        "EXISTING FAMILIES (name (kind): meaning):\n" + vocab + "\n\n"
        "NEW families already proposed in earlier batches (reuse the SAME name when the source is the same publisher/series):\n" + rn + "\n\n"
        "TASK: for EACH source below output one object:\n"
        '{"source": <exact key>, "family": <existing family name> | "NEW:<lowercase_snake_case_name>", '
        '"kind": one of ' + json.dumps(list(KINDS)) + ', "jurisdiction": <"federal"|"pennsylvania"|"new_jersey"|<state>|"international"|"unknown">, '
        '"confidence": 0.0-1.0, "why": <= 8 words}\n'
        "RULES: use an existing family ONLY when the source really is that publication (same code/reporter/publisher) — do not "
        "force a fit. Otherwise propose NEW:<name> naming the publication or document class (e.g. NEW:occ_handbook, "
        "NEW:caci_jury_instructions, NEW:federal_register, NEW:sec_filings, NEW:cfpb_guidance, NEW:irs_manual, NEW:scotus_briefs). "
        "Group by publisher/series, not by individual document. If the evidence does not show what the source is, set "
        'family "unclassified", kind "unclassified", confidence <= 0.4 and say why. Confidence >= 0.8 only when the key AND the '
        "excerpts agree.\n\nSOURCES:\n" + "\n".join(items) + "\n\nJSON array:")


def call_llm(prompt: str, max_tokens: int, timeout: int = 600) -> str:
    body = {"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.1, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(LLM_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"] or ""


RX_NEW = re.compile(r"^NEW:[a-z][a-z0-9_]{2,48}$")


def parse_strict(raw: str, wanted: set[str], vocab_names: set[str]) -> tuple[dict[str, dict], list[str]]:
    """-> ({source: proposal}, errors). Rejects malformed items; never guesses."""
    errs = []
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
    s, e = txt.find("["), txt.rfind("]")
    if s < 0 or e < 0: return {}, [f"no JSON array in reply ({len(raw)} chars)"]
    try:
        arr = json.loads(txt[s:e + 1])
    except json.JSONDecodeError as ex:
        # salvage: one malformed item (usually an unescaped quote inside "why") must not sink the batch — parse each
        # top-level {...} object on its own and reject only the broken ones
        arr, bad = [], 0
        for m in re.finditer(r"\{[^{}]*\}", txt[s:e + 1]):
            try: arr.append(json.loads(m.group(0)))
            except json.JSONDecodeError: bad += 1
        errs.append(f"json error: {ex} — salvaged {len(arr)} objects, {bad} rejected")
        if not arr: return {}, errs
    if not isinstance(arr, list): return {}, ["top-level not a list"]
    out = {}
    for it in arr:
        if not isinstance(it, dict): errs.append("item not an object"); continue
        src = it.get("source")
        if src not in wanted: errs.append(f"unknown source {src!r}"); continue
        fam = str(it.get("family", "")).strip()
        if fam.startswith("NEW:"):
            fam = "NEW:" + re.sub(r"[^a-z0-9_]+", "_", fam[4:].strip().lower()).strip("_")
            if not RX_NEW.match(fam): errs.append(f"{src}: bad NEW name {fam!r}"); continue
        elif fam not in vocab_names and fam != "unclassified":
            # the model often drops the NEW: prefix on its own coined names — a lowercase slug that is not an existing family
            # IS a NEW proposal (deterministic normalisation of the model's own vocabulary, not a guess about the source)
            slug = re.sub(r"[^a-z0-9_]+", "_", fam.lower()).strip("_")
            if RX_NEW.match("NEW:" + slug): fam = "NEW:" + slug
            else: errs.append(f"{src}: family {fam!r} not in vocabulary"); continue
        kind = str(it.get("kind", "")).strip().lower()
        if kind not in KINDS: errs.append(f"{src}: bad kind {kind!r}"); continue
        try:
            conf = float(it.get("confidence"))
        except (TypeError, ValueError):
            errs.append(f"{src}: bad confidence"); continue
        if not 0.0 <= conf <= 1.0: errs.append(f"{src}: confidence out of range"); continue
        why = re.sub(r"\s+", " ", str(it.get("why", ""))).strip()
        if len(why.split()) > 15: why = " ".join(why.split()[:15])
        jur = str(it.get("jurisdiction", "unknown")).strip().lower() or "unknown"
        out[src] = {"source": src, "family": fam, "kind": kind, "jurisdiction": jur, "confidence": round(conf, 3), "why": why}
    return out, errs


# ------------------------------------------------------------------------------------------------------------------------
# summary
# ------------------------------------------------------------------------------------------------------------------------
def write_summary(props: list[dict], n_total_sources: int, rows_by_source: dict[str, int]):
    latest = {}
    for p in props: latest[p["source"]] = p
    by_fam = defaultdict(list)
    for p in latest.values(): by_fam[p["family"]].append(p)
    lines = [f"# struct family proposals — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"sources proposed: {len(latest)} / {n_total_sources} unclassified · model {MODEL} · rows covered "
             f"{sum(rows_by_source.get(s, 0) for s in latest):,}", "",
             "| proposed family | sources | rows | mean conf | ≥0.8 | examples |", "|---|---|---|---|---|---|"]
    for fam, ps in sorted(by_fam.items(), key=lambda kv: -sum(rows_by_source.get(p['source'], 0) for p in kv[1])):
        rows = sum(rows_by_source.get(p["source"], 0) for p in ps)
        conf = sum(p["confidence"] for p in ps) / len(ps)
        hi = sum(1 for p in ps if p["confidence"] >= 0.8)
        ex = "; ".join(f"`{p['source'][:40]}`" for p in sorted(ps, key=lambda p: -rows_by_source.get(p["source"], 0))[:3])
        lines.append(f"| {fam} | {len(ps)} | {rows:,} | {conf:.2f} | {hi} | {ex} |")
    low = [p for p in latest.values() if p["confidence"] < 0.5]
    lines += ["", f"## unsure (confidence < 0.5): {len(low)} sources — NOT-IN-DATA candidates", ""]
    for p in sorted(low, key=lambda p: -rows_by_source.get(p["source"], 0))[:60]:
        lines.append(f"- `{p['source']}` ({rows_by_source.get(p['source'], 0):,} rows) → {p['family']} {p['confidence']}: {p['why']}")
    SUMMARY.write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--minutes", type=float, default=85.0, help="wall budget; stops launching new batches after this")
    ap.add_argument("--limit", type=int, default=0, help="max sources this run (0 = all remaining)")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=0, help="0 = 110 per source + 200")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if a.concurrency > 6: a.concurrency = 6                                       # server handles 8 streams; leave headroom

    con_s = sqlite3.connect(f"file:{STRUCT_DB}?mode=ro", uri=True)
    con_c = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
    cens = census(con_s)
    rows_by_source = dict(cens)
    done_props = load_jsonl(PROPOSALS)
    done = {p["source"] for p in done_props}
    if a.summary_only:
        write_summary(done_props, len(cens), rows_by_source); print(f"summary -> {SUMMARY}"); return

    todo = [(s, n) for s, n in cens if s not in done]
    if a.limit: todo = todo[:a.limit]
    print(f"unclassified sources {len(cens)} · already proposed {len(done)} · this run {len(todo)}", flush=True)

    ev_cache = {e["source"]: e for e in load_jsonl(EVIDENCE)}
    t0 = time.time()
    evs = []
    with EVIDENCE.open("a") as fh:
        for s, n in todo:
            e = ev_cache.get(s)
            if e is None:
                e = gather_evidence(con_s, con_c, s, n)
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            evs.append(e)
        fh.flush()
    print(f"evidence ready for {len(evs)} sources in {time.time() - t0:.0f}s", flush=True)

    batches = [evs[i:i + a.batch] for i in range(0, len(evs), a.batch)]
    vocab_names = {n for n, _k, _m in vocabulary()}
    lock = threading.Lock()
    running_new = Counter(p["family"] for p in done_props if p["family"].startswith("NEW:"))
    stats = Counter()
    deadline = t0 + a.minutes * 60
    max_tokens = a.max_tokens or (90 * a.batch + 200)

    def run_batch(bi: int, batch: list[dict]) -> tuple[int, int, list[str]]:
        wanted = {e["source"] for e in batch}
        with lock: rn = running_new.most_common(40)
        got, errs = {}, []
        for attempt in range(2):
            remaining = [e for e in batch if e["source"] not in got]
            if not remaining: break
            try:
                raw = call_llm(build_prompt(remaining, rn), max_tokens)
            except (urllib.error.URLError, TimeoutError, OSError) as ex:
                errs.append(f"attempt {attempt}: {ex}"); time.sleep(3); continue
            part, perr = parse_strict(raw, wanted, vocab_names)
            errs += perr
            got.update(part)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with lock:
            with PROPOSALS.open("a") as fh:
                for src, p in got.items():
                    p.update({"model": MODEL, "ts": ts, "n_rows": rows_by_source.get(src, 0)})
                    fh.write(json.dumps(p, ensure_ascii=False) + "\n")
                    if p["family"].startswith("NEW:"): running_new[p["family"]] += 1
            stats["ok"] += len(got); stats["missing"] += len(batch) - len(got); stats["batches"] += 1
        return bi, len(got), errs

    launched = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {}
        it = iter(enumerate(batches))
        def launch():
            nonlocal launched
            if time.time() > deadline: return False
            try: bi, b = next(it)
            except StopIteration: return False
            futs[ex.submit(run_batch, bi, b)] = bi; launched += 1; return True
        for _ in range(a.concurrency):
            if not launch(): break
        while futs:
            for f in as_completed(list(futs)):
                bi = futs.pop(f)
                try:
                    _bi, n_ok, errs = f.result()
                except Exception as exc:                                                       # keep the pass alive
                    n_ok, errs = 0, [f"batch {bi} crashed: {exc!r}"]
                el = time.time() - t0
                print(f"[{el / 60:5.1f}m] batch {bi + 1}/{len(batches)} ok={n_ok}/{len(batches[bi])} total_ok={stats['ok']} "
                      f"missing={stats['missing']}" + (f" errs={errs[:2]}" if errs else ""), flush=True)
                launch()
                break
    write_summary(load_jsonl(PROPOSALS), len(cens), rows_by_source)
    print(f"done: proposed {stats['ok']} · missing {stats['missing']} · batches {stats['batches']}/{len(batches)} launched {launched} "
          f"· {(time.time() - t0) / 60:.1f} min · summary -> {SUMMARY}")


if __name__ == "__main__":
    main()
