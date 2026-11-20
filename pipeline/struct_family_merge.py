#!/usr/bin/env python3
"""struct_family_merge.py — MERGE PASS over the LLM-proposed NEW struct families (SANDBOX, 2026-11-10).

Why: struct_family_propose.py names NEW families per batch, so one publisher/series fragments into siblings
(treasury_* x12, us_tax_treaties vs us_treaty_te, cfpb_rules/cfpb_rulemaking/cfpb_guidance ...). This pass gives the
model the WHOLE NEW-family set (names + row/source counts + 3 example sources + its own earlier 'why' lines) PLUS the
EXISTING vocabulary (FAMILIES names/kind/meaning) and asks, per family, where it belongs: an EXISTING family (only when
it is truly the same series — it must say why), a canonical NEW name shared by its siblings, or itself.
Deterministic = guardrail, LLM = comprehension: no keyword tables; the script only enforces rules.

Input families  : every family in chunk_struct that is not in FAMILIES/orphan (applied NEW families) PLUS every NEW:<name>
                  in struct_family_proposals.jsonl at confidence >= --pending-conf that is not applied yet (so the quorum
                  lift that follows uses canonical names).
Model           : local-gpu exl3 lane http://localhost:8000/v1 (qwen3.8-27b-exl3), thinking OFF, <= 60 families per call,
                  <= 6 streams. Strict JSON [{from, to, kind, confidence, why}] — never guessed, malformed items rejected.
Rules enforced  : no merge below --min-conf (0.8); kind CLASSES must agree (statute never merges into guidance; the
                  builder collapses document/guidance/form into kind 'document', so those three are one class);
                  'to' is an existing family, the family itself, or a snake_case name of <= 3 tokens; chains resolve
                  transitively (cycle -> the member with most rows); a merge into an EXISTING family is applied only for
                  sources family_of() would NOT already own — otherwise it is reported as 'builder_gap' (never updated).
Output          : output/research/struct_family_merge.jsonl  (one line per input family: from, to, resolved_to, kind,
                  confidence, why, status, rows, sources)  +  struct_family_merge.md (before/after + canonical groups)
Apply (--apply) : UPDATE chunk_struct SET family=resolved_to WHERE family=from  + one struct_family_audit row per source
                  (method 'merge'); then struct_family_apply.rebuild_override_tables(). Backup .bak.<ts> first.
                  Invariants: COUNT(*) unchanged (7,016,151); no row with family NULL/''.

Run : cd ~/beezle/_sandbox_struct && env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES /usr/bin/python3 \
          pipeline/struct_family_merge.py [--batch 60] [--concurrency 6] [--apply] [--reuse] [--dry-run]
      --reuse  = skip the model, re-apply rules to the raw replies cached in struct_family_merge.raw.jsonl
Verify: /usr/bin/python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" pipeline/struct_family_merge.py
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, re, shutil, sqlite3, sys, threading, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from struct_index_build import FAMILIES, FAM_INDEX, KIND_OF, ORPHAN, family_of          # noqa: E402
from struct_family_propose import vocabulary, call_llm, load_jsonl, KINDS, KIND_MAP      # noqa: E402
from struct_family_apply import rebuild_override_tables, ensure_audit                     # noqa: E402

SANDBOX = HERE.parent
STRUCT_DB = Path(os.environ.get("STRUCT_DB", str(SANDBOX / "struct_index.db")))
OUT_DIR = Path(os.environ.get("STRUCT_RESEARCH_DIR", str(SANDBOX / "output" / "research")))
PROPOSALS = OUT_DIR / "struct_family_proposals.jsonl"
MERGE_JSONL = OUT_DIR / "struct_family_merge.jsonl"
MERGE_RAW = OUT_DIR / "struct_family_merge.raw.jsonl"
MERGE_MD = OUT_DIR / "struct_family_merge.md"
LIVE = (_eroot() / "corpus/struct_index.db").resolve()
EXPECTED_ROWS = 7_016_151
MODEL = os.environ.get("BEEZLE_B70_MODEL", "qwen3.8-27b-exl3")

# kind CLASSES: the builder has one 'document' kind for everything non-codified, so guidance/form/document are one class.
KIND_CLASS = {"statute": "statute", "regulation": "regulation", "reg": "regulation", "rule": "rule", "local_rule": "rule",
              "case": "case", "treatise": "treatise", "dictionary": "dictionary", "document": "document",
              "guidance": "document", "form": "document", "unclassified": "document"}
RX_CANON = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){0,2}$")                      # snake_case, <= 3 tokens


def kind_class(k: str | None) -> str:
    return KIND_CLASS.get((k or "").lower(), "document")


# ------------------------------------------------------------------------------------------------------------------------
# input families (deterministic, read-only)
# ------------------------------------------------------------------------------------------------------------------------
def gather_families(con, pending_conf: float) -> dict[str, dict]:
    """{name: {name, status: applied|pending, rows, sources, kind, examples[3], why[3]}}"""
    latest = {}
    for p in load_jsonl(PROPOSALS): latest[p["source"]] = p
    applied_src = {r[0] for r in con.execute("SELECT DISTINCT source FROM struct_family_audit WHERE method LIKE 'llm_proposal%'")}
    fams: dict[str, dict] = {}
    # applied NEW families = what is in chunk_struct outside FAMILIES/orphan
    for fam, n, ns in con.execute("SELECT family, COUNT(*), COUNT(DISTINCT source) FROM chunk_struct GROUP BY family"):
        if fam in FAM_INDEX or fam == ORPHAN: continue
        fams[fam] = {"name": fam, "status": "applied", "rows": n, "sources": ns, "kinds": Counter(), "examples": [], "why": []}
        ex = con.execute("SELECT source, COUNT(*) c FROM chunk_struct WHERE family=? GROUP BY source ORDER BY c DESC LIMIT 3", (fam,)).fetchall()
        fams[fam]["examples"] = [s for s, _ in ex]
    # pending NEW names (below quorum) from the proposals, at the lift threshold
    pend_rows = defaultdict(list)
    for p in latest.values():
        if p["source"] in applied_src or not p["family"].startswith("NEW:") or p["confidence"] < pending_conf: continue
        pend_rows[p["family"][4:]].append(p)
    for name, ps in pend_rows.items():
        if name in fams: continue
        ps.sort(key=lambda p: -p.get("n_rows", 0))
        fams[name] = {"name": name, "status": "pending", "rows": sum(p.get("n_rows", 0) for p in ps), "sources": len(ps),
                      "kinds": Counter(), "examples": [p["source"] for p in ps[:3]], "why": []}
    # kinds + why lines from the model's own proposals (applied or pending)
    for p in latest.values():
        if not p["family"].startswith("NEW:"): continue
        f = fams.get(p["family"][4:])
        if f is None: continue
        f["kinds"][p.get("kind", "document")] += 1
        if p.get("why") and p["why"] not in f["why"] and len(f["why"]) < 3: f["why"].append(p["why"])
    for f in fams.values():
        f["kind"] = f["kinds"].most_common(1)[0][0] if f["kinds"] else "document"
        f["kinds"] = dict(f["kinds"])
    return fams


# ------------------------------------------------------------------------------------------------------------------------
# prompt + strict parse
# ------------------------------------------------------------------------------------------------------------------------
SYSTEM = ("You consolidate the FAMILY vocabulary of a legal text corpus index. A family = one publication, series, code, "
          "reporter or document class from one publisher. Earlier batches coined NEW family names independently, so the "
          "same series may carry several names. Answer with ONE JSON array and nothing else — no prose, no markdown fence.")


def build_prompt(batch: list[dict], all_names: list[dict], granularity: str = "series") -> str:
    vocab = "\n".join(f"- {n} ({k}): {m}" for n, k, m in vocabulary())
    names = ", ".join(f"{f['name']}({f['rows']})" for f in all_names)
    items = [json.dumps({"from": f["name"], "kind": f["kind"], "rows": f["rows"], "sources": f["sources"],
                         "example_sources": f["examples"], "earlier_why": f["why"]}, ensure_ascii=False) for f in batch]
    if granularity == "class":
        unit = ("A family = ONE PUBLISHER (agency, court, office) + ONE DOCUMENT CLASS (reports, rules_regulations, guidance, "
                "forms, orders, press, data, manuals, briefs, treaties, budgets, newsletters, hearings, letters). Different report "
                "TITLES from the same office are the SAME family (treasury_tax_expenditures, treasury_tax_analysis and "
                "treasury_tax_reports are all Treasury tax-policy reports -> one name); a series that is the same class under two "
                "spellings is one family (fincen_* / fin_cen_*). Keep classes apart: rules vs guidance vs press vs forms vs reports.")
        example = "(e.g. treasury_imf_* -> treasury_imf_reports; cfpb_rules + cfpb_rulemaking -> cfpb_rules; ots_* reports -> ots_reports)"
    else:
        unit = ("Merge by publisher AND document class — rules/regulations stay apart from guidance, press releases, forms, "
                "reports and datasets even from the same agency; never merge different kinds; a treaty series is not a statute code; "
                "an order list is not a journal unless the sources are the same series.")
        example = "(e.g. treasury_tax_expenditures + treasury_tax_analysis + treasury_tax_reports -> treasury_tax_reports)"
    return (
        "EXISTING FAMILIES (name (kind): meaning) — the deterministic vocabulary the index already builds:\n" + vocab + "\n\n"
        "ALL NEW FAMILY NAMES proposed so far (name(rows)) — pick canonical targets from these when siblings exist:\n" + names + "\n\n"
        "TASK: for EACH family below output one object {\"from\": <name>, \"to\": <target>, \"kind\": one of "
        + json.dumps(list(KINDS)) + ", \"confidence\": 0.0-1.0, \"why\": <= 12 words}.\n"
        "<target> is ONE of: (a) an EXISTING family name — ONLY when the NEW family is literally the same publication/series "
        "(same code, reporter, journal or publisher series; e.g. a second name for the Journal of the Supreme Court) and the "
        "'why' says which series; (b) a canonical NEW name (lowercase snake_case, at most 3 tokens joined by '_') shared by every "
        "sibling that is the same publisher + document class — reuse an existing NEW name from the list when one fits, otherwise "
        "coin the shortest name that covers the group " + example + "; (c) the family itself when it is already a coherent, distinct family.\n"
        "RULES: " + unit + " Confidence >= 0.8 only when the names, kinds "
        "and example sources show the same thing; below 0.8 the family is kept as is.\n\nFAMILIES:\n" + "\n".join(items) + "\n\nJSON array:")


def parse_strict(raw: str, wanted: set[str]) -> tuple[dict[str, dict], list[str]]:
    errs = []
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
    s, e = txt.find("["), txt.rfind("]")
    if s < 0 or e < 0: return {}, [f"no JSON array in reply ({len(raw)} chars)"]
    try:
        arr = json.loads(txt[s:e + 1])
    except json.JSONDecodeError as ex:
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
        frm = str(it.get("from", "")).strip()
        if frm not in wanted: errs.append(f"unknown from {frm!r}"); continue
        to = str(it.get("to", "")).strip()
        to = re.sub(r"^NEW:", "", to)
        to = re.sub(r"[^a-z0-9_]+", "_", to.lower()).strip("_")
        if not to: errs.append(f"{frm}: empty to"); continue
        kind = str(it.get("kind", "")).strip().lower()
        if kind not in KINDS: errs.append(f"{frm}: bad kind {kind!r}"); continue
        try: conf = float(it.get("confidence"))
        except (TypeError, ValueError): errs.append(f"{frm}: bad confidence"); continue
        if not 0.0 <= conf <= 1.0: errs.append(f"{frm}: confidence out of range"); continue
        why = re.sub(r"\s+", " ", str(it.get("why", ""))).strip()
        if len(why.split()) > 20: why = " ".join(why.split()[:20])
        out[frm] = {"from": frm, "to": to, "kind": kind, "confidence": round(conf, 3), "why": why}
    return out, errs


# ------------------------------------------------------------------------------------------------------------------------
# deterministic rules
# ------------------------------------------------------------------------------------------------------------------------
def apply_rules(fams: dict[str, dict], replies: dict[str, dict], min_conf: float) -> dict[str, dict]:
    """-> {from: {from,to,resolved_to,kind,confidence,why,status,rows,sources}}; 'to' after the guardrails, 'resolved_to'
    after transitive resolution. Status: self | merge_new | merge_existing | rejected:<reason> | no_reply"""
    out = {}
    for name, f in fams.items():
        r = replies.get(name)
        rec = {"from": name, "rows": f["rows"], "sources": f["sources"], "input_status": f["status"], "kind": f["kind"],
               "to": name, "confidence": None, "why": "", "status": "no_reply"}
        if r is None: out[name] = rec; continue
        rec.update({"confidence": r["confidence"], "why": r["why"], "kind_model": r["kind"]})
        to = r["to"]
        if to == name: rec["status"] = "self"; out[name] = rec; continue
        if r["confidence"] < min_conf: rec["status"] = "rejected:low_conf"; out[name] = rec; continue
        src_class = kind_class(f["kind"])
        if to in FAM_INDEX and to not in ("unclassified",):
            if kind_class(KIND_OF[to]) != src_class: rec["status"] = f"rejected:kind_mismatch({f['kind']}->{KIND_OF[to]})"; out[name] = rec; continue
            rec["to"] = to; rec["status"] = "merge_existing"; out[name] = rec; continue
        if to == "unclassified" or to == ORPHAN: rec["status"] = "rejected:catch_all_target"; out[name] = rec; continue
        if not RX_CANON.match(to) and to not in fams: rec["status"] = f"rejected:bad_name({to})"; out[name] = rec; continue   # coined names <= 3 tokens; names already in the data pass
        tgt = fams.get(to)
        tgt_kind = tgt["kind"] if tgt else r["kind"]
        if kind_class(tgt_kind) != src_class: rec["status"] = f"rejected:kind_mismatch({f['kind']}->{tgt_kind})"; out[name] = rec; continue
        rec["to"] = to; rec["status"] = "merge_new"; out[name] = rec
    # transitive resolution (a -> b -> c => a -> c); cycles resolve to the member with most rows
    def resolve(n: str) -> str:
        seen = []
        cur = n
        while cur in out and out[cur]["to"] != cur and cur not in seen and len(seen) < 8:
            seen.append(cur); cur = out[cur]["to"]
        if cur in seen:                                                          # cycle
            cyc = seen[seen.index(cur):]
            cur = max(cyc, key=lambda x: fams[x]["rows"] if x in fams else 0)
        return cur
    for name, rec in out.items():
        rt = resolve(name)
        rec["resolved_to"] = rt
        if rt == name and rec["status"].startswith("merge"): rec["status"] = "self"      # cycle collapsed onto itself
        elif rt != rec["to"] and rec["status"].startswith("merge"): rec["status"] += ":chained"
        if rt in FAM_INDEX: rec["kind_final"] = KIND_MAP.get(KIND_OF[rt], KIND_OF[rt])
    # canonical kind for each NEW target = majority kind over its members (weighted by rows)
    members = defaultdict(list)
    for name, rec in out.items(): members[rec["resolved_to"]].append(name)
    for tgt, ms in members.items():
        if tgt in FAM_INDEX: continue
        kc = Counter()
        for m in ms: kc[fams[m]["kind"] if m in fams else out[m]["kind"]] += fams[m]["rows"] if m in fams else 1
        k = kc.most_common(1)[0][0]
        for m in ms: out[m]["kind_final"] = k
    return out


def builder_gap_check(con, recs: dict[str, dict]) -> dict[str, dict]:
    """For merges into EXISTING families: sources family_of() already owns are 'builder_gap' (reported, never updated)."""
    gaps = {}
    for name, rec in recs.items():
        if rec["resolved_to"] not in FAM_INDEX or not rec["status"].startswith("merge"): continue
        own = []
        for (s,) in con.execute("SELECT DISTINCT source FROM chunk_struct WHERE family=?", (name,)):
            if family_of(s)[0] == rec["resolved_to"]: own.append(s)
        if own: gaps[name] = {"to": rec["resolved_to"], "sources_owned_by_family_of": own[:20], "n": len(own)}
    return gaps


def apply_merges(con, recs: dict[str, dict], gaps: dict[str, dict], dry: bool) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    stat = Counter(); rows_moved = 0
    for name, rec in sorted(recs.items()):
        if not rec["status"].startswith("merge") or rec["input_status"] != "applied": continue
        to = rec["resolved_to"]
        skip = set(gaps.get(name, {}).get("sources_owned_by_family_of", []))
        srcs = [s for (s,) in con.execute("SELECT DISTINCT source FROM chunk_struct WHERE family=?", (name,))]
        for s in srcs:
            if s in skip: stat["builder_gap_sources"] += 1; continue
            n = con.execute("SELECT COUNT(*) FROM chunk_struct WHERE source=? AND family=?", (s, name)).fetchone()[0]
            if not dry:
                con.execute("UPDATE chunk_struct SET family=? WHERE source=? AND family=?", (to, s, name))
                con.execute("INSERT INTO struct_family_audit VALUES (?,?,?,?,?,?,?,?,?)",
                            (s, name, to, rec["confidence"], rec["why"], ts, None, rec.get("kind_final", rec["kind"]), "merge"))
            rows_moved += n; stat["sources_moved"] += 1
        stat["families_merged"] += 1
    stat["rows_moved"] = rows_moved
    return dict(stat)


def write_outputs(recs: dict[str, dict], gaps: dict, fams: dict, before: dict, after: dict | None, stat: dict | None):
    with MERGE_JSONL.open("w") as fh:
        for name in sorted(recs): fh.write(json.dumps(recs[name], ensure_ascii=False) + "\n")
    groups = defaultdict(list)
    for name, rec in recs.items(): groups[rec["resolved_to"]].append(rec)
    n_new_before = len([f for f in fams.values() if f["status"] == "applied"])
    rows_before = sum(f["rows"] for f in fams.values() if f["status"] == "applied")
    new_after = {t for t, ms in groups.items() if t not in FAM_INDEX and any(m["input_status"] == "applied" for m in ms)}
    lines = [f"# struct family merge — {time.strftime('%Y-%m-%d %H:%M')} · model {MODEL}", "",
             f"input families: {len(fams)} ({n_new_before} applied in chunk_struct / {len(fams) - n_new_before} pending below quorum)",
             f"applied NEW families BEFORE: {n_new_before} ({rows_before:,} rows) · AFTER (projected by map): {len(new_after)} NEW + "
             f"{len([t for t in groups if t in FAM_INDEX])} folded into EXISTING families",
             f"statuses: {dict(Counter(r['status'].split(':')[0] + (':' + r['status'].split(':')[1] if r['status'].startswith('rejected') else '') for r in recs.values()))}",
             f"builder_gap (family_of already owns the source — NOT updated): {sum(g['n'] for g in gaps.values())} sources in {len(gaps)} families",
             f"DB before: {json.dumps(before)}", f"DB after: {json.dumps(after)}" if after else "DB after: (not applied)",
             f"apply stat: {json.dumps(stat)}" if stat else "", "",
             "| canonical target | kind | members | rows | member families (rows) |", "|---|---|---|---|---|"]
    for t, ms in sorted(groups.items(), key=lambda kv: -sum(m["rows"] for m in kv[1])):
        if len(ms) == 1 and ms[0]["status"] in ("self", "no_reply") or ms[0]["status"].startswith("rejected") and len(ms) == 1: continue
        rows = sum(m["rows"] for m in ms)
        kind = ms[0].get("kind_final", ms[0]["kind"])
        mem = "; ".join(f"`{m['from']}`({m['rows']:,}){'*' if m['input_status']=='pending' else ''}" for m in sorted(ms, key=lambda m: -m["rows"]))
        lines.append(f"| {'**' + t + '**' if t in FAM_INDEX else t} | {kind} | {len(ms)} | {rows:,} | {mem} |")
    lines += ["", "`*` = pending (below quorum, not yet in chunk_struct); **bold** = EXISTING family", "",
              "## merges into EXISTING families (why)", ""]
    for name, rec in sorted(recs.items()):
        if rec["resolved_to"] in FAM_INDEX and rec["status"].startswith("merge"):
            lines.append(f"- `{name}` ({rec['rows']:,}) -> **{rec['resolved_to']}** {rec['confidence']}: {rec['why']}")
    lines += ["", "## rejected / unmerged (rule guardrails)", ""]
    for name, rec in sorted(recs.items()):
        if rec["status"].startswith("rejected") or rec["status"] == "no_reply":
            lines.append(f"- `{name}` ({rec['rows']:,}) -> `{rec['to']}` {rec['confidence']}: {rec['status']} — {rec['why']}")
    if gaps:
        lines += ["", "## builder gap (family_of already owns these sources; re-index, do not merge)", ""]
        for name, g in gaps.items(): lines.append(f"- `{name}` -> {g['to']}: {g['n']} sources e.g. {g['sources_owned_by_family_of'][:3]}")
    MERGE_MD.write_text("\n".join(lines) + "\n")


def counts(con) -> dict:
    r = con.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM chunk_struct WHERE family='unclassified'").fetchone()
    new = [(f, n) for f, n in con.execute("SELECT family, COUNT(*) FROM chunk_struct GROUP BY family") if f not in FAM_INDEX and f != ORPHAN]
    return {"total_rows": con.execute("SELECT COUNT(*) FROM chunk_struct").fetchone()[0], "unclassified_rows": r[0], "unclassified_sources": r[1],
            "new_families": len(new), "new_family_rows": sum(n for _, n in new)}


def fold_prior(fams: dict[str, dict], prior: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Round N input: fold the prior round's map into the family set — each prior target becomes one input family whose
    rows/sources/examples/why aggregate its members. -> (fams2, map1 {original name: prior target})."""
    map1 = {r["from"]: r["resolved_to"] for r in prior if r.get("resolved_to")}
    fams2: dict[str, dict] = {}
    for name, f in fams.items():
        t = map1.get(name, name)
        if t in FAM_INDEX: continue                                            # already folded into an existing family
        g = fams2.get(t)
        if g is None:
            g = fams2[t] = {"name": t, "status": "pending", "rows": 0, "sources": 0, "kinds": Counter(), "examples": [], "why": [], "members": []}
        g["rows"] += f["rows"]; g["sources"] += f["sources"]; g["members"].append(name)
        if f["status"] == "applied": g["status"] = "applied"
        for k, c in (f.get("kinds") or {}).items(): g["kinds"][k] += c
        for e in f["examples"]:
            if e not in g["examples"] and len(g["examples"]) < 3: g["examples"].append(e)
        for w in f["why"]:
            if w not in g["why"] and len(g["why"]) < 3: g["why"].append(w)
    for g in fams2.values():
        g["kind"] = g["kinds"].most_common(1)[0][0] if g["kinds"] else "document"; g["kinds"] = dict(g["kinds"])
    return fams2, map1


def compose(fams: dict[str, dict], map1: dict[str, str], recs2: dict[str, dict], why1: dict[str, str] | None = None) -> dict[str, dict]:
    """Final records for the ORIGINAL names: from -> (round1 target) -> (round2 resolved target)."""
    out = {}; why1 = why1 or {}
    for name, f in fams.items():
        t1 = map1.get(name, name)
        r2 = recs2.get(t1)
        final = r2["resolved_to"] if r2 else t1
        why = (r2["why"] if r2 and final != t1 else "") or (why1.get(name, "") if t1 != name else "")
        rec = {"from": name, "rows": f["rows"], "sources": f["sources"], "input_status": f["status"], "kind": f["kind"], "to": t1,
               "resolved_to": final, "confidence": r2["confidence"] if r2 else None, "why": why,
               "status": "self" if final == name else ("merge_existing" if final in FAM_INDEX else "merge_new")}
        if r2 and r2["status"].startswith("rejected") and final == name: rec["status"] = r2["status"]
        if r2 and r2.get("kind_final"): rec["kind_final"] = r2["kind_final"]
        elif final in FAM_INDEX: rec["kind_final"] = KIND_MAP.get(KIND_OF[final], KIND_OF[final])
        out[name] = rec
    return out


def pack_by_prefix(fams: dict[str, dict], batch: int) -> list[list[dict]]:
    """Batches that keep every publisher-prefix group (first snake token) in ONE prompt, <= batch families each."""
    groups = defaultdict(list)
    for f in fams.values(): groups[f["name"].split("_")[0]].append(f)
    out, cur = [], []
    for _k, g in sorted(groups.items()):
        g.sort(key=lambda f: f["name"])
        if cur and len(cur) + len(g) > batch: out.append(cur); cur = []
        cur.extend(g)
        while len(cur) > batch: out.append(cur[:batch]); cur = cur[batch:]
    if cur: out.append(cur)
    return out


# ------------------------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=60); ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--min-conf", type=float, default=0.8); ap.add_argument("--pending-conf", type=float, default=0.85)
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="re-run the rules on cached raw replies (no model call)")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--granularity", choices=["series", "class"], default="series", help="series = same publication; class = publisher + document class")
    ap.add_argument("--prior", default="", help="previous round's struct_family_merge.jsonl: this round runs on its targets and the maps compose")
    ap.add_argument("--tag", default="", help="suffix for the raw-reply cache (struct_family_merge.raw<tag>.jsonl)")
    a = ap.parse_args()
    if STRUCT_DB.resolve() == LIVE: sys.exit("REFUSED: STRUCT_DB is the live turbovec index")
    a.batch = min(a.batch, 60); a.concurrency = min(a.concurrency, 6)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = MERGE_RAW if not a.tag else OUT_DIR / f"struct_family_merge.raw{a.tag}.jsonl"
    con = sqlite3.connect(str(STRUCT_DB), timeout=120); con.execute("PRAGMA journal_mode=WAL"); ensure_audit(con)
    before = counts(con); print("before:", json.dumps(before), flush=True)
    fams0 = gather_families(con, a.pending_conf)
    print(f"input families {len(fams0)}: applied {sum(1 for f in fams0.values() if f['status']=='applied')} · pending {sum(1 for f in fams0.values() if f['status']=='pending')}", flush=True)
    map1: dict[str, str] = {}; why1: dict[str, str] = {}
    fams = fams0
    if a.prior:
        prior = load_jsonl(Path(a.prior)); why1 = {r["from"]: r.get("why", "") for r in prior}
        fams, map1 = fold_prior(fams0, prior)
        print(f"prior map folded: {len(map1)} names -> {len(fams)} round-input families", flush=True)

    replies: dict[str, dict] = {}
    if a.reuse and raw_path.exists():
        for r in load_jsonl(raw_path):
            part, _e = parse_strict(r["raw"], set(r["wanted"])); replies.update(part)
        print(f"reused {len(replies)} replies from {raw_path.name}", flush=True)
    else:
        all_names = sorted(fams.values(), key=lambda f: -f["rows"])
        if a.granularity == "class": batches = pack_by_prefix(fams, a.batch)
        else:
            order = sorted(fams.values(), key=lambda f: f["name"])               # alphabetical: siblings share a batch
            batches = [order[i:i + a.batch] for i in range(0, len(order), a.batch)]
        lock = threading.Lock(); t0 = time.time()
        raw_path.write_text("")

        def run(bi, batch):
            wanted = {f["name"] for f in batch}; got, errs = {}, []
            for attempt in range(2):
                rem = [f for f in batch if f["name"] not in got]
                if not rem: break
                try: raw = call_llm(build_prompt(rem, all_names, a.granularity), 80 * len(rem) + 200)
                except (urllib.error.URLError, TimeoutError, OSError) as ex:
                    errs.append(f"attempt {attempt}: {ex}"); time.sleep(3); continue
                part, perr = parse_strict(raw, wanted); errs += perr; got.update(part)
                with lock:
                    with raw_path.open("a") as fh: fh.write(json.dumps({"batch": bi, "wanted": sorted(wanted), "raw": raw}) + "\n")
            return bi, got, errs
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = [ex.submit(run, i, b) for i, b in enumerate(batches)]
            for f in as_completed(futs):
                bi, got, errs = f.result(); replies.update(got)
                print(f"[{(time.time()-t0)/60:4.1f}m] batch {bi+1}/{len(batches)} ok={len(got)}/{len(batches[bi])}" + (f" errs={errs[:2]}" if errs else ""), flush=True)
    recs = apply_rules(fams, replies, a.min_conf)
    if a.prior: recs = compose(fams0, map1, recs, why1); fams = fams0
    gaps = builder_gap_check(con, recs)
    st = Counter(r["status"].split(":")[0] for r in recs.values())
    print("rules:", dict(st), "· builder_gap families:", len(gaps), flush=True)
    stat = after = None
    if a.apply:
        if not a.dry_run and not a.no_backup:
            bak = STRUCT_DB.with_name(STRUCT_DB.name + f".bak.{time.strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(STRUCT_DB, bak); print(f"backup -> {bak}", flush=True)
        stat = apply_merges(con, recs, gaps, a.dry_run)
        if not a.dry_run:
            ov = rebuild_override_tables(con); stat["override_tables"] = ov
            con.commit()
        after = counts(con)
        bad = con.execute("SELECT COUNT(*) FROM chunk_struct WHERE family IS NULL OR family=''").fetchone()[0]
        print("apply:", json.dumps(stat)); print("after:", json.dumps(after))
        if after["total_rows"] != EXPECTED_ROWS or bad: sys.exit("INVARIANT VIOLATION — inspect the backup")
    write_outputs(recs, gaps, fams, before, after, stat)
    print(f"wrote {MERGE_JSONL} + {MERGE_MD}")


if __name__ == "__main__":
    main()
