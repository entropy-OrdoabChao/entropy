"""GROUND PACK — the ONE deterministic pre-pass for a room (agnostic: court + filing type + case dir).
Assembles, once, everything the bots must not re-derive:
  1. adverse authorities → verbatim chunks (ground_table.py; statutes/rules via the m0 rules_index first)
  2. FORUM RULES for this court + filing type (forum_rules.json cell) → verbatim text from the corpus
     (Pa. Code t231 chapters direct; m0 rules_index for statutes); missing families are named NOT IN CORPUS
  3. filing requirements cell (pipeline.filing_requirements)
  4. FACE table (face_table.py)
  5. m0 grounding extras that return non-empty (definitions, levers, maxims/equity, duties)
usage: ground_pack.py <adverse.md> <verified_record.md> <out.md> --court PA_CP_PHILA --type sur_reply [--case <case_id>]"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, sys, sqlite3, argparse
sys.path.insert(0, str(_epkg())); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ground_table, face_table

HERE = os.path.dirname(os.path.abspath(__file__)); ADV_NAME = "adverse.md"
FORUM = json.load(open(f"{HERE}/forum_rules.json"))


def _db():
    from pipeline.case_comprehension import CHUNK_DB
    return sqlite3.connect(str(CHUNK_DB))


def pa_rule_text(num, sub="", why=""):
    """Pa.R.C.P. <num>: the t231 chapter chunk that carries the heading 'Rule <num>. ' (sliced there). For a
    subsection cite, the chapter chunk carrying that subsection marker whose text best matches the cell's
    'why' (chunks are unordered hashes, so the subsection is located by content, not position)."""
    c = _db()
    rows = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE ? AND text LIKE ? LIMIT 5", (f"pa_code_t231_ch{num}%", f"%Rule {num}.%")).fetchall()
    rows += c.execute("SELECT source, text FROM chunk_text WHERE source LIKE 'pa_code_t231_ch%' AND text LIKE ? LIMIT 20", (f"%Rule {num}. %",)).fetchall()
    def chapter_ok(s):
        m = re.search(r"ch(\d+)", s); ch = int(m.group(1)) if m else -1
        return 0 <= int(num) - ch < 1000
    rows = [r for r in rows if chapter_ok(r[0])]
    head, src = None, None
    for s, t in rows:
        i = t.find(f"Rule {num}. ")
        if i >= 0 and not re.match(r"Rule \d+\. .{0,60}(Rescinded|Reserved)", t[i:]):
            head, src = t[i:], s; break
    if not head:
        c.close(); return None, None
    heading = head[:head.find(". ", 6) + 2] if ". " in head[:200] else head[:80]
    body = head[:2400]
    if sub:
        first = sub.split(")(")[0].strip("()")
        mark = re.compile(rf"(?<![A-Za-z0-9])\({re.escape(first)}\) ?[A-Z(]")
        cands = [(src, head)] + [r for r in c.execute("SELECT source, text FROM chunk_text WHERE source=? AND text LIKE ?", (src, f"%({first})%")).fetchall()]
        ww = set(re.findall(r"[a-z]{5,}", why.lower()))
        best, bs = None, -1
        for s2, t2 in cands:
            for m in mark.finditer(t2):
                seg = t2[m.start():m.start() + 1700]
                # a segment belongs to another rule if a different rule heading sits between the chunk start and the marker
                before = t2[:m.start()]
                other = re.findall(r"Rule (\d+)\. [A-Z]", before)
                if other and other[-1] != str(num): continue
                sc = len(ww & set(re.findall(r"[a-z]{5,}", seg.lower()))) + (3 if f"Rule {num}" in t2 else 0)
                if sc > bs: bs, best = sc, seg
        if best: body = heading + "… " + best
    c.close()
    return " ".join(body.split()), src


def phila_rule_text(num):
    c = _db()
    key = "phila_cr_" + re.sub(r"[^0-9a-z]", "", num.lower())
    rows = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE ? ORDER BY source", (key + "_%",)).fetchall()
    rows = [r for r in rows if re.match(rf"{re.escape(key)}_[a-z]", r[0])]      # exact rule key: 1028c_… not 10281_…
    if not rows and "(" not in num:                                            # bare number → every subsection of that rule
        rows = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE ? ORDER BY source", (key + "%",)).fetchall()
        rows = [r for r in rows if re.match(rf"{re.escape(key)}[a-z]?_[a-z]", r[0])]
    if rows:                                                    # consolidated FJD compiled rules (ingest_phila_local_rules)
        body = " ".join(" ".join(t.split()) for _, t in rows[:3]); c.close(); return body[:4000], rows[0][0]
    try:
        c.execute("PRAGMA query_only=1"); c.set_progress_handler(lambda: 1, 200_000_000)   # ~bounded scan; give up rather than hang
        rows = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE '%\\_cts' ESCAPE '\\' AND text LIKE ? LIMIT 6", (f"%Rule *{num}%",)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    c.close()
    for s, t in rows:
        i = t.find(f"Rule *{num}")
        if i >= 0: return " ".join(t[i:i + 900].split()), s + "  (Pa. Bulletin fragment — Philadelphia Civil Rules NOT IN CORPUS as a clean family: ingest gap)"
    return None, None


def resolve_rule(cite, why=""):
    m = re.match(r"Pa\.R\.C\.P\. ?(\d+)((?:\([a-z0-9]+\))*)", cite)
    if m: return pa_rule_text(m.group(1), m.group(2), why)
    m = re.match(r"Phila\. ?Civ\. ?R\. ?\*?(\d+(?:\.\d+)?(?:\([a-z0-9]+\))*)", cite)
    if m: return phila_rule_text(m.group(1))
    if re.search(r"U\.S\.C|Fed\. ?R\.|L\.A\.R\.|I\.O\.P\.", cite):
        # FEDERAL: the promoted m2 cite fetch (turbovec main+delta, pinned to the authority's own source, rule-subsection
        # window). The cell's 'why' may lead with the subsection ('(c)(7) …') — appended so the window ranks it first.
        try:
            from pipeline.m2 import analysis_runner as AR
            sub = (re.match(r"\s*((?:\([a-zA-Z0-9]{1,3}\))+)", why or "") or [None, ""])[1]
            hits = [h for h in AR._pull_cites_tv(cite + sub, per=1) if h.get("chunk_id")]
            if hits: return " ".join(hits[0]["text"].split()), hits[0]["chunk_id"] + "  (m2 pinned fetch)"
            return None, "NOT IN CORPUS by m2 semantic+pin fetch"
        except Exception as e:
            return None, f"m2 fetch error {e}"
    try:
        from pipeline.rules_index import lookup
        h = lookup(cite, limit=1)
        if h and h[0].get("body"): return " ".join(h[0]["body"].split())[:1800], h[0].get("source", "")
    except Exception as e:
        return None, f"rules_index error {e}"
    return None, None


def forum_section(court, ftype):
    cell = FORUM.get(court, {})
    if ftype == "_all":      # S-m2-blind-posture: no vehicle chosen -> every rule cell for this court, deduped, so the room sees all doors
        seen, rules = set(), []
        for k, rs in cell.items():
            for r in (rs or []):
                key = (r.get("cite") if isinstance(r, dict) else str(r))     # one corpus resolve per cite; the first cell's `why` stands
                if key not in seen:
                    seen.add(key); rules.append(r)
    else:
        rules = cell.get(ftype) or cell.get("_default") or []
    out = [f"## FORUM RULES — {court} / {ftype} (deterministic cell; verbatim text; quote ONLY these words)"]
    gaps = []
    resolved_cites = []
    for r in rules:
        cite, why = r["cite"], r.get("why", "")
        body, src = resolve_rule(cite, why)
        if body:
            cid = _chunk_for(cite, src)
            out.append(f"### R. {cite} — {why}\nsource: {src}\nchunk: {cid or 'NONE'}\n> {body[:RULE_CHARS]}\n")
            resolved_cites.append((cite, body))
        else: gaps.append(f"- {cite} — {why}: NOT IN CORPUS (no verbatim text; do not quote or paraphrase it as if quoted){'; ' + src if src else ''}")
    if gaps: out.append("### NOT RESOLVED\n" + "\n".join(gaps))
    if os.environ.get("GP_DEEPEN", "1") == "1":
        out.append(deepen_section(resolved_cites))
    return "\n".join(out) + "\n", len(rules), len(gaps)


# per-rule verbatim budget. The chain passes GROUND_PACK_RULE_CHARS=700 (62-rule _all cell x 1700 + DEEPEN = 106 KB, over the
# room's 64 KB opening cap -> "opening message cannot fit cap", RELAY EXIT 1). 700 keeps the operative sentence(s); the lane
# retrieves the full text by chunk/source when it quotes.
RULE_CHARS = int(os.environ.get("GROUND_PACK_RULE_CHARS", "1700"))
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _chunk_for(cite, src):
    """The corpus chunk id a rule's text is citable as [corpus:<id>]. rules_index/pa_rule_text return a source label, not a
    chunk — pin one by the m2 cite fetch so the posture gate's unquoted-authority check can pass on forum rows."""
    head = (src.split()[0] if isinstance(src, str) and src.strip() else "")
    if _HEX32.match(head) or re.search(r"_chunk_\d+$", head):
        return head
    try:
        from pipeline.m2 import analysis_runner as AR
        num = re.findall(r"\d+[a-z]?", cite)
        title = re.match(r"\s*(\d+)\s+Pa\.C\.S", cite)
        for h in AR._pull_cites_tv(cite, per=3):
            t = h.get("text", "")
            if not h.get("chunk_id") or not num or num[-1] not in t:
                continue
            if title and not re.search(rf"\b{title.group(1)} Pa\.? ?C\.? ?S", t):
                continue   # 42 Pa.C.S. § 702 must not pin to 45 Pa.C.S. § 702(7)
            return h["chunk_id"]
        return None
    except Exception:
        return None


def deepen_section(cites, hops=2, k=6, cap_per=6, cap_total=48):
    """DEEPEN — the depth layer m0 built (issue_spotter DEEPEN round over corpus_graph.co_cited_deep) that nobody wired into
    the room. For every resolved forum rule: its corpus co-citation neighbourhood 2 hops out — the authorities the law itself
    cross-references beside that rule — each pinned to a chunk so a lane cites it as [corpus:<id>]. This is how the room sees
    past the forum cell to the connected doors (§ 702(b) beside 341(c), 1311 beside 702(b), 227.1 beside 1028) unprompted."""
    try:
        from pipeline import corpus_graph as cg
        from pipeline.m2 import analysis_runner as AR
    except Exception as e:
        return f"## DEEPEN — unavailable ({e})\n"
    out = ["## DEEPEN — doctrine neighbourhood of the forum rules (1: the cites the rule's OWN TEXT names; 2: corpus co-citation, 2 hops; cite ONLY with the chunk shown)"]
    pairs = [(c, "") if isinstance(c, str) else (c[0], c[1] or "") for c in cites]
    seen, total = {c for c, _ in pairs}, 0
    for cite, body in pairs:
        try:
            canon = [cc for _t, cc in cg.authorities_in(cite)] or [cite]
            # source 1 — the rule's own cross-references (341 Official Note names 311/312/1311; 1311 names § 702(b)): exact,
            # deterministic, weight 1000 so they rank above co-citation. This is the walk that finds the connected DOORS.
            neigh = [(x, 1000.0) for _t, x in cg.authorities_in(body) if x not in canon]
            if not neigh:   # co-citation is the FALLBACK only: when the rule's own text names nothing (it is noisy for PA procedure)
                for cc in canon:
                    neigh += [(x, w) for x, w in cg.co_cited_deep(cc, hops=hops, k=k)]
        except Exception:
            continue
        rows = []
        # family filter: a state-court forum keeps PaCS / PA rules / cases / Pub.L.; CFR neighbours are Pa. Bulletin co-print
        # noise unless the forum is federal (GP_DEEPEN_FAMILIES overrides; "CFR" is in the set by default only for federal courts)
        fams = set((os.environ.get("GP_DEEPEN_FAMILIES") or ("CASE,PaCS,PA_RULE,PUBL,USC" + (",CFR" if os.environ.get("GP_COURT_FEDERAL") == "1" else ""))).split(","))
        for x, w in sorted(neigh, key=lambda t: -t[1]):
            if x in seen or len(rows) >= cap_per:
                continue
            xt = [t for t, _ in cg.authorities_in(x)]
            if xt and not (set(xt) & fams):
                continue
            seen.add(x)
            # pin by the SAME resolver the forum rows use (pa_rule_text / phila / rules_index), then the corpus chunk id;
            # the raw m2 cite fetch mis-pins PA cites (42 Pa.C.S. § 702 -> a 45 Pa.C.S. § 702(7) Bulletin line)
            body_x, src_x = resolve_rule(x, "")
            cid = _chunk_for(x, src_x) if body_x else None
            if body_x and cid:
                rows.append(f"- {x}  chunk: {cid}  (w={w:.2f})\n  > {' '.join(body_x.split())[:min(420, RULE_CHARS)]}")
            elif body_x:
                rows.append(f"- {x}  chunk: NONE (text resolved via {src_x}; no corpus chunk id — quote the forum-row text, not this)\n  > {' '.join(body_x.split())[:300]}")
            else:
                rows.append(f"- {x}  chunk: NONE — named by the corpus graph, no verbatim chunk pinned; NOT quotable")
            total += 1
        if rows:
            out.append(f"### near {cite}\n" + "\n".join(rows))
        if total >= cap_total:
            out.append(f"(capped at {cap_total} neighbours)"); break
    return "\n".join(out) + "\n"


def grounds_section(adverse):
    """the adverse filing's own argument headings = the grounds every relay must answer (coverage is checked against these)."""
    heads = []
    for i, l in enumerate(adverse.splitlines(), 1):
        t = l.strip()
        if re.match(r"^(#{1,4} |[A-Z]\. |[IVX]{1,4}\. |\d{1,2}\. [A-Z])", t) and 12 <= len(t) <= 220 and not re.match(r"^\d+\. (?:Exhibit|See)", t):
            heads.append((i, re.sub(r"^#+ ", "", t)))
    if not heads: return ""
    return "## GROUNDS — the adverse filing's own argument headings (every one must be answered; skeptic reports COVERAGE per ground)\n" + "\n".join(f"G{n}. {h}  ({os.path.basename(ADV_NAME)}:{i})" for n, (i, h) in enumerate(heads, 1)) + "\n"


def requirements_section(court, ftype):
    try:
        from pipeline import filing_requirements as FR
        reqs = FR.requirements(ftype, court)
        return "## FILING REQUIREMENTS CELL (pipeline.filing_requirements)\n" + "\n".join(f"- {r.get('level')}: {r.get('label')} — {r.get('cite')} {r.get('note', '')}" for r in reqs) + "\n"
    except Exception as e:
        return f"## FILING REQUIREMENTS CELL — unavailable ({e})\n"


def m0_extras(case_id, adverse):
    out = []
    try:
        from pipeline import drafter_grounding as G
        for name, fn in [("definitions", lambda: G.get_definition_grounding(case_id, adverse, max_notes=4)),
                         ("levers", lambda: G.get_spotted_levers_grounding(case_id, adverse, max_levers=8)),
                         ("maxims/equity", lambda: G.get_maxim_equity_grounding(adverse, max_items=4)),
                         ("rule text cited by the adverse filing", lambda: G.get_rule_grounding(adverse, max_rules=8))]:
            try: t = (fn() or "").strip()
            except Exception as e: t = ""
            if t: out.append(f"## m0 {name}\n{t[:3000]}\n")
    except Exception as e:
        out.append(f"## m0 grounding unavailable ({e})\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("adverse"); ap.add_argument("verified"); ap.add_argument("out")
    ap.add_argument("--court", required=True); ap.add_argument("--type", required=True); ap.add_argument("--case", default="")
    a = ap.parse_args()
    d = os.path.dirname(a.out) or "."
    gt = ground_table.build(a.adverse, f"{d}/ground_table.md")
    ft = face_table.build(a.adverse, a.verified, f"{d}/face_table.md")
    forum, nrules, ngaps = forum_section(a.court, a.type)
    adverse = open(a.adverse, errors="ignore").read()
    global ADV_NAME; ADV_NAME = a.adverse
    parts = [f"# GROUND PACK — {a.court} / {a.type} — built deterministically; bots reason on it, never re-fetch it\n",
             open(f"{d}/ground_table.md").read(), grounds_section(adverse), forum, requirements_section(a.court, a.type), open(f"{d}/face_table.md").read(),
             m0_extras(a.case, adverse) if a.case else ""]
    open(a.out, "w").write("\n".join(p for p in parts if p))
    print(json.dumps({"adverse_cites": gt["cites"], "resolved": gt["resolved"], "forum_rules": nrules, "forum_gaps": ngaps,
                      "face_rows": ft["rows"], "face_defects": ft["defects"], "bytes": os.path.getsize(a.out), "file": a.out}))


if __name__ == "__main__":
    main()
