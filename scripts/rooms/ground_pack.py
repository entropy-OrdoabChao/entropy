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

# ---------------------------------------------------------------- DELTA MEMO (user-locked 2026-11-14: "the delta could do it in less")
# Every resolve below is a pure function of (its inputs, the corpus). The memo lives beside the pack (<out_dir>/.gp_memo/) keyed by
# sha(inputs + corpus stamp): a forum_rules.json edit that adds 4 rules costs 4 resolves, not the whole 20-rule cell + DEEPEN.
import hashlib as _hl
_MEMO_DIR = None
_CORPUS_STAMP = None
def _corpus_stamp():
    global _CORPUS_STAMP
    if _CORPUS_STAMP is None:
        try: st = os.stat(str(_eroot() / "corpus/chunk_text.db")); _CORPUS_STAMP = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError: _CORPUS_STAMP = "nocorpus"
    return _CORPUS_STAMP
def _memo(name, key_parts, fn):
    """return fn() — served from <memo_dir>/<name>-<sha>.json when the same inputs were resolved against the same corpus."""
    if not _MEMO_DIR or os.environ.get("GP_MEMO", "1") == "0":
        return fn()
    sha = _hl.sha256(("\x1f".join(str(k) for k in key_parts) + "\x1f" + _corpus_stamp()).encode()).hexdigest()[:24]
    f = f"{_MEMO_DIR}/{name}-{sha}.json"
    try:
        if os.path.exists(f):
            return json.load(open(f))["v"]
    except Exception:
        pass
    v = fn()
    try:
        os.makedirs(_MEMO_DIR, exist_ok=True); json.dump({"v": v, "key": [str(k)[:200] for k in key_parts]}, open(f, "w"))
    except Exception:
        pass
    return v


def _db():
    from pipeline.case_comprehension import CHUNK_DB
    return sqlite3.connect(str(CHUNK_DB))


def pa_rule_text(num, sub="", why="", title="t231", full=False):
    """Pa.R.C.P. <num>: the t231 chapter chunk that carries the heading 'Rule <num>. ' (sliced there). For a
    subsection cite, the chapter chunk carrying that subsection marker whose text best matches the cell's
    'why' (chunks are unordered hashes, so the subsection is located by content, not position)."""
    c = _db()
    rows = []
    if title != "t231":
        # t210/t201: the chapter is the rule's hundreds — read that ONE source by equality (indexed). The LIKE-prefix scans below
        # walk the whole chunk table (~20 s a rule); the _all cell (62 rules) timed out at 1,362 s on them (S-room-depth regress).
        # 'Rule {num}.%' with NO trailing space: the Code's own heading is 'Rule 341.\u00a0 Final Orders' (NBSP) — a '. ' pattern
        # missed the true heading chunk and read a partial one (341(d)(3) → 903 lost; S-room-depth regress, vacate bundle)
        rows = c.execute("SELECT source, text FROM chunk_text WHERE source = ? AND text LIKE ?", (f"pa_code_{title}_ch{int(num) // 100}", f"%Rule {num}.%")).fetchall()
    if not rows:
        rows = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE ? AND text LIKE ? LIMIT 5", (f"pa_code_{title}_ch{num}%", f"%Rule {num}.%")).fetchall()
        rows += c.execute(f"SELECT source, text FROM chunk_text WHERE source LIKE 'pa_code_{title}_ch%' AND text LIKE ? LIMIT 20", (f"%Rule {num}. %",)).fetchall()
    def chapter_ok(s):
        m = re.search(r"ch(\d+)", s); ch = int(m.group(1)) if m else -1
        if title == "t231":
            return 0 <= int(num) - ch < 1000
        # t210 (Pa.R.A.P.) / t201 (Pa.R.J.A.): chapter = the rule's hundreds (1931 -> ch19, 121 -> ch1, 2101 -> ch21)
        return ch == int(num) // 100
    rows = [r for r in rows if chapter_ok(r[0])]
    # t210/t201 chunks carry NBSP runs ('Rule 341.\u00a0 Final', '(a)\u00a0\u00a0 General') that defeat every '. ' / ') ' marker
    # below — collapse whitespace once at read (t231 left byte-for-byte as before: its rows are the regression baseline)
    nz = (lambda t: " ".join(t.split())) if title != "t231" else (lambda t: t)
    rows = [(r[0], nz(r[1])) for r in rows]
    head, src = None, None
    for s, t in rows:
        i = t.find(f"Rule {num}. ")
        if i >= 0 and not re.match(r"Rule \d+\. .{0,60}(Rescinded|Reserved)", t[i:]):
            # S-room-depth: a chapter table of contents ('Rule 2101. Conformance with Requirements. 2102. Intervenors. …')
            # carries the heading but no rule text — two other rule numbers as headings right after it = an index, skip it
            # (index entries = bare following numbers, 1-50 above this one, not preceded by 'Rule'/'R.C.P.' — a cross-reference
            #  like 'under Rule 3101. The' is not an index; Rule 3111's own text cites 3101)
            nxt = [int(x) for x in re.findall(r"(?<!Rule )(?<!P\. )(?<!\d)(\d{3,4})\. [A-Z][a-z]", t[i + 6:i + 320])]
            if title != "t231" and len([x for x in nxt if 0 < x - int(num) <= 50]) >= 2:
                continue        # (t231 keeps its shipped first-hit pick — regression baseline)
            if title == "t231":
                head, src = t[i:], s; break
            # t210/t201: the rule's text is split over OVERLAPPING chunks that each carry the heading — keep the one holding the
            # most of THIS rule (heading → next rule's heading / chunk end); first-hit read 341 without (d), losing 341(d)(3)→903
            nx = re.search(rf"Rule (?!{num}\.)\d+(?:\.\d+)?\. [A-Z]", t[i + 6:])
            span = t[i: i + 6 + nx.start()] if nx else t[i:]
            if head is None or len(span) > len(head): head, src = span, s
    if not head:
        c.close(); return None, None
    heading = head[:head.find(". ", 6) + 2] if ". " in head[:200] else head[:80]
    body = head if full else head[:2400]
    if sub:
        levels = [x for x in re.split(r"\)\(", sub.strip("()")) if x]
        first = levels[0]; second = levels[1] if len(levels) > 1 else None
        mark = re.compile(rf"(?<![A-Za-z0-9])\({re.escape(first)}\) ?[A-Z(]")
        cands = [(src, head)] + [(r[0], nz(r[1])) for r in c.execute("SELECT source, text FROM chunk_text WHERE source=? AND text LIKE ?", (src, f"%({first})%")).fetchall()]
        ww = set(re.findall(r"[a-z]{5,}", why.lower()))
        best, bs = None, -1
        for s2, t2 in cands:
            for m in mark.finditer(t2):
                seg = t2[m.start():m.start() + 1700]
                # a segment belongs to another rule if a different rule heading sits between the chunk start and the marker
                before = t2[:m.start()]
                other = re.findall(r"Rule (\d+)\. [A-Z]", before)
                if other and other[-1] != str(num): continue
                if title == "t231":
                    # Pa.R.C.P.: the scoring this resolver shipped with, unchanged — t231 rows are the regression baseline
                    # (the depth scoring below re-picked 1028(c)(2) onto another rule's '(c)'; S-room-depth regress, all341c)
                    sc = len(ww & set(re.findall(r"[a-z]{5,}", seg.lower()))) + (3 if f"Rule {num}" in t2 else 0)
                    if sc > bs: bs, best = sc, seg
                    continue
                # Pa.R.A.P. / Pa.R.J.A. (S-room-depth):
                # commentary names a subdivision ('Subdivision (a)(8) tracks …', 'paragraph (a)(1) provides …') — not the rule
                if re.search(r"(?:[Ss]ubdivisions?|[Pp]aragraphs?|[Ss]ubparagraphs?|Rule \d+|P\. \d+|\band|\bor|,)\s*$", before[-30:]):
                    continue
                # +3 only for the rule's own HEADING ('Rule 1925. O…'), never a cross-reference ('Pa.R.A.P. 1925(b)') — a chunk of
                # Rule 1941 that cross-cites 1925 out-ranked the real 1925(c) Remand chunk (S-room-depth); +4 when the subsection's
                # own caption ('(c) Remand.') is a word of the cell's 'why'
                sc = len(ww & set(re.findall(r"[a-z]{5,}", seg.lower()))) + (3 if re.search(rf"Rule {num}\. [A-Z]", t2) else 0)
                cap = re.match(rf"\({re.escape(first)}\) ?([A-Z][a-z]+(?: [a-z]+){{0,3}})[.—]", seg)
                if cap and set(re.findall(r"[a-z]{5,}", cap.group(1).lower())) & ww: sc += 4
                # S-room-depth: an explanatory-comment marker ('(c)(1): This subparagraph applies …') is commentary, not the rule
                if re.match(rf"\({re.escape(first)}\)\(\w+\):", seg) or re.search(r"(Note|Comment)[:.]?\s*$", before[-40:]):
                    sc -= 10
                if second:   # (c)(2): the segment must carry the second-level marker; the body is cut to it, the (c) lead kept
                    j = seg.find(f"({second}) ", 3)
                    if 0 < j < 1500:
                        sc += 5; seg = seg[:min(j, 160)].rstrip() + (" … " if j > 160 else " ") + seg[j:]
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
    # S-room-depth: Pa.R.A.P. (t210) and Pa.R.J.A. (t201) by the same chapter + subsection resolver as Pa.R.C.P. — before this
    # they fell to rules_index (no subsection window: 1925(c) returned 1925(a); Pa.R.J.A. 108 returned 49 U.S.C. § 108).
    m = re.match(r"Pa\. ?R\. ?A\. ?P\. ?(\d+)((?:\([a-z0-9]+\))*)", cite)
    if m:
        b, s = pa_rule_text(m.group(1), m.group(2), why, title="t210")
        if b: return b, s
    m = re.match(r"Pa\. ?R\. ?J\. ?A\. ?(?:No\. )?(\d+)((?:\([a-z0-9]+\))*)", cite)
    if m:
        return pa_rule_text(m.group(1), m.group(2), why, title="t201")
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
        if h and h[0].get("body"):
            src0 = str(h[0].get("source", ""))
            # family guard (S-room-depth): a Pennsylvania cite never resolves to a federal source (Pa.R.J.A. 108 -> usc_t49_s108)
            if re.search(r"\bPa\.|Pa\.C\.S|P\.S\.", cite) and re.match(r"(usc_|cfr_|fed_)", src0):
                return None, f"rules_index returned {src0} for a PA cite — rejected (family mismatch)"
            return " ".join(h[0]["body"].split())[:1800], src0
    except Exception as e:
        return None, f"rules_index error {e}"
    return None, None


def _src_family_ok(cite, src):
    """a resolved source must belong to the cite's own family: 'N Pa.C.S.' -> pa_consolidated_titleN; a reporter cite
    (A.2d / Pa. / U.S.) never to a statute/regulation source; a PA rule never to usc_/cfr_."""
    s0 = str(src or "").split()[0] if str(src or "").strip() else ""
    m = re.match(r"(\d{1,2}) ?Pa\. ?C\. ?S\.", cite)
    if m:
        t = re.match(r"pa_consolidated_title(\d+)", s0)
        return bool(t) and t.group(1) == m.group(1)
    if re.search(r"\d \S*(?:A\.(?:2d|3d)?|U\.S\.|Pa\.(?: Super\.)?|F\.(?:2d|3d|4th)?) \d", cite) and re.match(r"(usc_|cfr_|pa_code_|pa_consolidated)", s0):
        return False
    if re.match(r"Pa\. ?R\.", cite) and re.match(r"(usc_|cfr_)", s0):
        return False
    return True


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
        body, src, cid = _memo("rule", [cite, why, RULE_CHARS], lambda: (lambda b, sr: (b, sr, _chunk_for(cite, sr) if b else None))(*resolve_rule(cite, why)))
        if body:
            out.append(f"### R. {cite} — {why}\nsource: {src}\nchunk: {cid or 'NONE'}\n> {body[:RULE_CHARS]}\n")
            resolved_cites.append((cite, body))
        else: gaps.append(f"- {cite} — {why}: NOT IN CORPUS (no verbatim text; do not quote or paraphrase it as if quoted){'; ' + src if src else ''}")
    if gaps: out.append("### NOT RESOLVED\n" + "\n".join(gaps))
    if os.environ.get("GP_DEEPEN", "1") == "1":
        out.append(_memo("deepen", [c for c, _ in resolved_cites], lambda: deepen_section(resolved_cites)))
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
            xbody = body
            if re.match(r"Pa\. ?R\. ?(?:A\. ?P|J\. ?A)\.", cite) and "(" in cite:
                # S-room-depth: a subsection slice ('341(c)') drops the cross-references the rule's other subdivisions carry
                # (341 -> 903); the bare rule's text is added for the walk only — the row still quotes the subsection
                _m = re.match(r"Pa\. ?R\. ?(A\. ?P|J\. ?A)\. ?(?:No\. )?(\d+)", cite)
                xbody = body + " " + ((pa_rule_text(_m.group(2), "", "", title="t210" if _m.group(1).startswith("A") else "t201", full=True)[0] or "") if _m else "")
            neigh = [(x, 1000.0) for _t, x in cg.authorities_in(xbody) if x not in canon]
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
            if body_x and not _src_family_ok(x, src_x):
                # S-room-depth: the fuller rule text names more neighbours, and the fallback resolver mis-pins some
                # ('2 Pa.C.S. § 702' -> pa_consolidated_title27 'Agricultural Advisory Board'; '803 A.2d 767' -> usc_t16_s803).
                # Wrong verbatim is worse than none: keep the name, drop the text.
                body_x, src_x = None, None
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


# ---------------------------------------------------------------------------------------------------------------------------
# S-room-depth (2026-10-30): the UNITS layer. Before this the pack resolved only the adverse paper's cites + the forum cell; an
# order that cites nothing (0000 EDA: ORDERS 06-09/06-17) gave the room an EMPTY authority table, and every rule our own units
# turn on (3111(b), 3142, 3146(a), 311(a)(2), 1925(c)(2) …) reached the lanes only as words — they quoted them from memory.
# Opt-in by --points (the chain passes OUR_ISSUES + ADVERSE_POINTS); without it the pack is byte-identical to before.
_UNIT_RULE = re.compile(r"(Pa\.\s?R\.\s?(?:C\.\s?P|A\.\s?P|J\.\s?A)\.\s?\d{1,4}(?:\.\d+)?(?:\([a-z0-9]{1,4}\))*"
                        r"|\d{1,2}\s?Pa\.\s?C\.\s?S\.\s?§+\s?\d+(?:\.\d+)?(?:\([a-z0-9]{1,4}\))*)")
_UNIT_CASE = re.compile(r"([A-Z][\w.'&-]*(?: (?:of|the|and|&|[A-Z][\w.'&-]*)){0,6} v\. [A-Z][\w.'&-]*(?: (?:of|the|and|&|[A-Z][\w.'&-]*)){0,6},"
                        r" \d{1,4} (?:A\.(?:2d|3d)?|U\.S\.|Pa\.(?: Super\.| Cmwlth\.)?|F\.(?:2d|3d|4th)?|S\. ?Ct\.) \d{1,5})")
_NOT_OPINION = ("pa_code", "pa_consolidated", "pa_bulletin", "usc_", "cfr_", "amjur", "def_", "treatise", "cjs_", "plaw_",
                "courtlistener_pa_code", "phila_cr", "frcp", "frap", "fre_", "law_", "comps", "blackstone", "bouvier")


def _norm_cite(c):
    """'Pa. R.C.P.  3146(a)' -> 'Pa.R.C.P. 3146(a)'; '42 Pa.C.S.§§5505' -> '42 Pa.C.S. § 5505' (the forum-cell spelling)."""
    c = " ".join(c.split())
    m = re.match(r"Pa\.\s?R\.\s?(C|A|J)\.\s?(P|A)\.\s?(.+)$", c)
    if m: return f"Pa.R.{m.group(1)}.{m.group(2)}. {m.group(3).strip()}"
    m = re.match(r"(\d{1,2})\s?Pa\.\s?C\.\s?S\.\s?§+\s?(.+)$", c)
    if m: return f"{m.group(1)} Pa.C.S. § {m.group(2).strip()}"
    return c


def _ctx(text, i, n=220):
    a = max(0, text.rfind("\n", 0, i) + 1); return " ".join(text[a:a + n].split())


def units_section(points_text, skip=(), cap_rules=24, cap_cases=10, caselaw_rules=8):
    """every rule / statute / case OUR units name → verbatim corpus text + chunk (same resolvers as the forum rows), then, for
    each resolved PA rule, the opinions that APPLY it — semantic search seeded by the rule's own text, kept only when the
    opinion names the rule number (the corpus co-citation graph holds almost no opinion edges for the 3100-series)."""
    skipn = {_norm_cite(x) for x in skip}
    rules, seen = [], set()
    for m in _UNIT_RULE.finditer(points_text):
        c = _norm_cite(m.group(1))
        if c in seen or c in skipn: continue
        seen.add(c); rules.append((c, _ctx(points_text, m.start())))
    cases, cseen = [], set()
    for m in _UNIT_CASE.finditer(points_text):
        c = " ".join(m.group(1).split())
        k = re.search(r"\d+ \S+(?: \S+)? \d+$", c).group(0)
        if k in cseen: continue
        cseen.add(k); cases.append((c, _ctx(points_text, m.start())))
    out = [f"## UNIT AUTHORITIES — every rule / statute / case OUR units name ({len(rules)} rules, {len(cases)} cases; "
           f"verbatim corpus text; quote ONLY these words, cite the chunk shown)"]
    gaps, resolved = [], []
    for cite, ctx in rules[:cap_rules]:
        body, src = resolve_rule(cite, ctx)
        if body:
            cid = _chunk_for(cite, src)
            out.append(f"### U. {cite}\nnamed in: {ctx[:160]}\nsource: {src}\nchunk: {cid or 'NONE'}\n> {body[:min(420, RULE_CHARS)]}\n")
            resolved.append((cite, body))
        else:
            gaps.append(f"- {cite}: NOT IN CORPUS (no verbatim text){'; ' + src if src else ''}")
    for cite, ctx in cases[:cap_cases]:
        try:
            hit, _ = ground_table.resolve(cite, ctx)
        except Exception as e:
            hit = None
        if hit and hit.get("text"):
            out.append(f"### U. {cite}\nchunk: {hit.get('chunk_id')}\n> {' '.join(hit['text'].split())[:min(420, RULE_CHARS)]}\n")
        else:
            gaps.append(f"- {cite}: NOT RESOLVED to the opinion's own text")
    if gaps: out.append("### UNIT AUTHORITIES NOT RESOLVED\n" + "\n".join(gaps))
    # case law that applies each PA rule, seeded by the rule's own words
    try:
        from pipeline.m2 import analysis_runner as AR
        rows = []
        for cite, body in [r for r in resolved if r[0].startswith("Pa.R.")][:caselaw_rules]:
            num = re.search(r"(\d{2,4})", cite).group(1)
            hits = AR._retrieve([f"{cite} {' '.join(body.split())[:300]}"], [], k=12)
            got = []
            for h in hits:
                src = (h.get("source") or "").lower()
                if h.get("error") or not h.get("chunk_id") or src.startswith(_NOT_OPINION) or re.match(r"\d", src): continue
                t = h.get("text") or ""
                if not re.search(rf"\b(?:Rule|R\.\s?(?:C|A)\.\s?P\.)\s?{num}\b", t): continue
                got.append(f"- {h.get('source')}  chunk: {h['chunk_id']}  (applies {cite}; mode={h.get('mode')})\n  > {' '.join(t.split())[:360]}")
                if len(got) >= 2: break
            rows += [f"### opinions applying {cite}"] + (got or [f"- none in corpus naming Rule {num} near its text — NOT IN CORPUS as applied case law (pull+ingest candidate)"])
        if rows: out.append("## CASE LAW APPLYING THE UNIT RULES (semantic, seeded by the rule's own text; the opinion must name the rule)\n" + "\n".join(rows))
    except Exception as e:
        out.append(f"## CASE LAW APPLYING THE UNIT RULES — unavailable ({e})")
    return "\n".join(out) + "\n"


_TREATISE_PREFIX = ("amjur", "cjs_", "def_amjur", "encyc_amjur", "amjur_proof")   # the treatise shelf: Am Jur 2d / POF / C.J.S.


def treatise_section(points_text, per_unit=2, cap=14, min_score=0.0):
    """S-amjur-shelf (2026-11-14): per unit, the treatise paragraphs (Am Jur 2d, Proof of Facts, C.J.S.) that state the
    governing rule + elements of proof — the memorandum-of-law shelf. Same engine as derived_rules_section, scoped to the
    treatise sources; semantic, seeded by the unit's own title+body. Class fix: the pack swept only the forum's RULE families
    (pa_rcp/pa_rap), so a PO/brief was drafted without 30 Am Jur 2d Evidence § 959 / 1 Abatement § 20 / 20 Courts § 144 /
    54A Mortgages although all were held and on point. A unit with no treatise hit is reported as such (no silent gap)."""
    try:
        from pipeline.m2 import analysis_runner as AR
    except Exception as e:
        return f"## TREATISE SHELF — unavailable ({e})\n"
    out = [f"## TREATISE SHELF — Am Jur 2d / Proof of Facts / C.J.S. per unit (the memorandum-of-law layer: governing rule + elements "
           f"of proof; quote ONLY these words, cite the chunk shown; NEVER as the binding authority — pair each with the rule/statute/holding)"]
    n = 0
    for title, body in _units(points_text):
        if n >= cap: break
        q = f"{title} {body[:400]}"
        try:   # scoped to the shelf (every amjur_*/cjs_ family in FAMILIES.yaml) so the treatise text competes only with itself
            fams = [k for k in AR._fam_registry() if k.startswith(("amjur_", "cjs_"))]
            hits = AR._retrieve([q], fams, k=per_unit * 4)
        except Exception as e:
            out.append(f"### {title[:120]}\n- treatise retrieval failed ({e})"); continue
        got = []
        for h in hits:
            src = (h.get("source") or "").lower()
            if h.get("error") or not h.get("chunk_id") or not src.startswith(_TREATISE_PREFIX): continue
            if float(h.get("score") or 0) < min_score: continue
            t = " ".join((h.get("text") or "").split())
            if len(t) < 120: continue
            sec = re.search(r"§+\s?[\d.]+[a-z]?", t)
            got.append(f"- {h.get('source')}  chunk: {h['chunk_id']}{'  ' + sec.group(0) if sec else ''}\n  > {t[:420]}")
            if len(got) >= per_unit: break
        out.append(f"### {title[:120]}\n" + ("\n".join(got) if got else
                   "- no treatise paragraph in corpus for this unit — NOT ON SHELF (Am Jur title to pull+ingest; name the title in the gap line)"))
        n += 1
    return "\n".join(out) + "\n"


def _units(points_text, cap=12):
    """the unit blocks of a points file: '**N. title**' (the cells_to_stub unit form) or '## title' headings -> [(title, body)]."""
    # a unit title may run past one line's 200 chars and close its ** later (0000 EDA units 1-3) — the title is the bold
    # line up to its closing ** or the line end, cut to 200
    rx = re.compile(r"(?m)^(?:\*\*\s*(\d{1,3}\.\s+[^\n]{6,}?)(?:\*\*|$)|#{2,4} ([^\n]{6,200}?)\s*$)")
    ms = list(rx.finditer(points_text)); out = []
    for i, m in enumerate(ms):
        body = points_text[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(points_text)]
        out.append(((m.group(1) or m.group(2)).strip()[:200], " ".join(body.split())))
    return out[:cap]


def derived_rules_section(points_text, court, skip=(), per_unit=4, cap=14):
    """ENGINE, not a list: for every unit, the corpus decides which rules of THIS forum's rule families govern it — semantic search
    of the unit's own words over the forum's rule family (forum_rules.json _rule_families: data), the rule number read from the
    hit's own heading, ranked by how many units reach it. The forum cell is only the seed/guardrail; this is what finds the
    rule nobody listed (1935 delinquency notice beside a 1931 record default)."""
    fams = (FORUM.get("_rule_families") or {}).get(court) or []
    if not fams: return ""
    try:
        from pipeline.m2 import analysis_runner as AR
    except Exception as e:
        return f"## DERIVED FORUM RULES — unavailable ({e})\n"
    pref = {"pa_rap": "Pa.R.A.P.", "pa_rja": "Pa.R.J.A.", "pa_rcp": "Pa.R.C.P.", "frap": "Fed. R. App. P.", "frcp": "Fed. R. Civ. P."}
    _b = lambda c: re.sub(r"\(.*$", "", _norm_cite(c)).strip()        # 'Pa.R.A.P. 2111(a)' and 'Pa.R.A.P. 2111' are one rule
    skipn = {_b(x) for x in skip}
    found = {}   # cite -> {units, score, chunk, text, source}
    for title, body in _units(points_text):
        q = f"{title}. {body[:400]}"
        for fam in fams:
            for h in AR._retrieve([q], [fam], k=per_unit):
                if h.get("error") or not h.get("chunk_id"): continue
                src = (h.get("source") or "")
                if hasattr(AR, "_fam_match") and not AR._fam_match(src, fam): continue   # _retrieve falls back unscoped on a thin family
                t = " ".join((h.get("text") or "").split())
                m = re.search(r"\bRule (\d{1,4}(?:\.\d+)?)\. [A-Z]", t)
                if not m: continue
                cite = f"{pref.get(fam, fam)} {m.group(1)}"
                if _b(cite) in skipn: continue
                f = found.setdefault(cite, {"units": set(), "score": 0.0, "chunk": h["chunk_id"], "text": t[m.start():], "source": src})
                f["units"].add(title[:60]); f["score"] = max(f["score"], float(h.get("score") or 0))
    if not found: return ""
    ranked = sorted(found.items(), key=lambda kv: (-len(kv[1]["units"]), -kv[1]["score"]))[:cap]
    out = [f"## DERIVED FORUM RULES — the corpus's answer to 'which rules of this forum govern each unit' (semantic over {', '.join(fams)}; "
           f"rule number read from the hit's own heading; verbatim). CANDIDATES, ranked by units reached: a lane cites one only after "
           f"stating from its text why it governs THIS proceeding; skeptic: a D-rule cited without that → OPEN | APPLICABILITY"]
    for cite, f in ranked:
        out.append(f"### D. {cite}\nreached by {len(f['units'])} unit(s): {'; '.join(sorted(f['units']))[:200]}\nsource: {f['source']}\nchunk: {f['chunk']}\n> {f['text'][:min(420, RULE_CHARS)]}\n")
    return "\n".join(out) + "\n"


def fit_depth(text, budget=None):
    """S-room-depth: the depth layers (DERIVED / UNIT AUTHORITIES / CASE LAW / VAULT) sit after DEEPEN, where relay_room.fit_core
    never trims (it squeezes only the '### R.' forum quotes) — an unbounded depth block made the 0000 EDA pack 65,412 B against a
    52 KB core budget (RELAY EXIT before any lane). Bounded here, at the source: verbatim lines squeezed 420 → 300 → 200 → 120,
    then the lowest-ranked rows of each section dropped from the bottom; every step keeps the cite + chunk so a lane can retrieve."""
    budget = int(os.environ.get("GP_DEPTH_BUDGET", "22000")) if budget is None else budget
    if len(text.encode()) <= budget: return text
    for n in (420, 300, 200, 120):
        cand = re.sub(r"(?m)^(\s*> )(.*)$", lambda m: m.group(1) + (m.group(2)[:n] + ("…" if len(m.group(2)) > n else "")), text)
        if len(cand.encode()) <= budget: return cand
    text = cand
    blocks = re.split(r"(?m)(?=^### )", text)
    while len("".join(blocks).encode()) > budget and len(blocks) > 1:
        blocks.pop()                     # rows are emitted best-first within each section: the tail goes first
    return "".join(blocks).rstrip() + "\n(depth layer capped at GP_DEPTH_BUDGET; remaining rows dropped — retrieve by cite)\n"


# the room's opening budget (gateway hosted_room_discussion.MAX_USER_TEXT_BYTES 64 KiB - relay ROOM_CORE_RESERVE 12 KiB)
_ROOM_BUDGET = 64 * 1024 - int(os.environ.get("ROOM_CORE_RESERVE", str(12 * 1024)))


def _squeezed_size(core, n):
    """bytes of `core` after relay_room.fit_core's squeeze(n): '### R.' verbatim lines cut to n chars, everything from
    '## DEEPEN' on untouched (mirrors relay_room.fit_core — keep the two in step)."""
    i = core.find("## DEEPEN"); forum, rest = (core[:i], core[i:]) if i >= 0 else (core, "")
    out = []
    for b in re.split(r"(?=^### R\. )", forum, flags=re.M):
        out.append(b if not b.startswith("### R. ") else
                   re.sub(r"(?m)^> (.*)$", lambda m: "> " + (m.group(1)[:n] + ("…" if len(m.group(1)) > n else "")), b))
    return len(("".join(out) + rest).encode())


def vault_section(points_text, max_units=8, per_unit=1100, cap=8000):
    """the Obsidian layer (connected_notes.graph_context: semantic seeds + [[links]] walked 2 hops) per unit heading — what the
    vault already learned about each point. CONTEXT, not authority: a lane cites the corpus chunk a note names, never the note."""
    try:
        from pipeline import connected_notes as CN
    except Exception as e:
        return f"## VAULT CONTEXT — unavailable ({e})\n"
    heads = [t for t, _b in _units(points_text, cap=max_units)]
    if not heads:
        heads = [" ".join(points_text.split())[:300]]
    out, total = ["## VAULT CONTEXT — accumulated notes per unit ([[slug]]; context only — cite the corpus chunk the note names, never the note)"], 0
    for h in heads:
        try:
            g = (CN.graph_context(h, k=3, max_chars=per_unit) or "").strip()
        except Exception as e:
            g = ""
        if not g: continue
        # a note's own markdown headings ('## Scope of This Study') would read as pack sections to the relay's splitters
        # (fit_core cuts at '## DEEPEN', room_inputs slices on '### ') — demote them to bold lines
        g = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"**\1**", g)
        out.append(f"### unit: {h[:120]}\n{g}")
        total += len(g)
        if total >= cap:
            out.append("(vault context capped)"); break
    return "\n".join(out) + "\n" if len(out) > 1 else ""


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
    ap.add_argument("--points", default="", help="comma-separated unit files (OUR_ISSUES, ADVERSE_POINTS) — S-room-depth units + vault layers")
    a = ap.parse_args()
    d = os.path.dirname(a.out) or "."
    global _MEMO_DIR; _MEMO_DIR = f"{d}/.gp_memo"
    gt = ground_table.build(a.adverse, f"{d}/ground_table.md")
    ft = face_table.build(a.adverse, a.verified, f"{d}/face_table.md")
    forum, nrules, ngaps = forum_section(a.court, a.type)
    adverse = open(a.adverse, errors="ignore").read()
    global ADV_NAME; ADV_NAME = a.adverse
    units = vault = ""
    gt_md, gsec, req, face_md = open(f"{d}/ground_table.md").read(), grounds_section(adverse), requirements_section(a.court, a.type), open(f"{d}/face_table.md").read()
    m0x = m0_extras(a.case, adverse) if a.case else ""
    # CONNECTED RECORD (backlog connected_record, 2026-11-13): room_inputs writes <inputs>/connected_record.md — the record of
    # the cases this case's CASE_FACTS links, resolved from the [[wikilinks]] a tool-less lane cannot follow. dispatch_prompt's
    # lean CORE slice is this pack from '## FORUM RULES' to '# FACE TABLE' and record.md is NOT pasted in lean mode, so the
    # section goes INSIDE that window (right before the FACE TABLE) — every lane, producer and grader, carries it. No file
    # (no CONNECTED RECORD heading, or ROOM_CONNECTED_IN_PACK=0) → the pack is byte-identical to before.
    conn = ""
    if os.environ.get("ROOM_CONNECTED_IN_PACK", "1") != "0" and os.path.exists(f"{d}/connected_record.md"):
        try:
            sys.path.insert(0, HERE)
            import connected_record
            conn = connected_record.pack_section(open(f"{d}/connected_record.md", errors="ignore").read())
        except Exception as e:
            conn = f"## CONNECTED RECORD (resolved) — unavailable ({type(e).__name__}: {str(e)[:120]}); see connected_record.md\n"
    if a.points:
        pts = "\n".join(open(f, errors="ignore").read() for f in a.points.split(",") if f and os.path.exists(f))
        if pts.strip():
            # one budget for the whole depth layer, bounded by what the ROOM can still carry: the relay's fit_core never trims
            # anything after '## DEEPEN', so the depth layer gets only the headroom left once the forum quotes are squeezed to
            # 250 chars (never forcing the INDEX form). A 62-rule _all cell (all341c: 51.9 KB fitted) leaves ~0 → depth omitted.
            base = "\n".join(x for x in [gt_md, gsec, forum, req, conn, face_md, m0x] if x)
            room = _ROOM_BUDGET - _squeezed_size(base, 250) - 2048
            depth_budget = max(0, min(int(os.environ.get("GP_DEPTH_BUDGET", "22000")), room))
            if depth_budget < 3000:
                units = (f"## UNIT AUTHORITIES — omitted: the forum cell leaves {max(room, 0)} B of room headroom "
                         f"(retrieve each unit's authority by cite)\n")
            else:
                cell = FORUM.get(a.court, {}); frules = cell.get(a.type) or cell.get("_default") or []
                fskip = [r["cite"] for r in frules if isinstance(r, dict)]
                units = _memo("units", [pts, fskip], lambda: units_section(pts, skip=fskip))
                if os.environ.get("GP_DERIVE", "1") == "1":
                    units = _memo("derived", [pts, a.court, fskip], lambda: derived_rules_section(pts, a.court, skip=fskip + _UNIT_RULE.findall(pts))) + units
                if os.environ.get("GP_TREATISE", "1") == "1":   # S-amjur-shelf: the memo-of-law layer rides with the rules
                    units = units + _memo("treatise", [pts], lambda: treatise_section(pts))
                if os.environ.get("GP_VAULT", "1") == "1":
                    vault = _memo("vault", [pts], lambda: vault_section(pts))
                vb = min(int(os.environ.get("GP_VAULT_BUDGET", "4000")), depth_budget // 5)   # context gives way first
                if len(vault.encode()) > vb:
                    vault = vault.encode()[:vb].decode(errors="ignore").rsplit("\n", 1)[0] + "\n(vault context capped)\n"
                units = fit_depth(units, depth_budget - len(vault.encode()))
    # S-rebuttal-table (2026-11-01): per-adverse-cite REBUTTAL ROWS + LAW-FORM / JURISDICTION-FORM / RECORD-CLOSURE axes. Opt-in
    # (GP_REBUTTAL=1) until the live room proves it; memoized per (cite, corpus_version, model) so a rebuild costs only new cites.
    # Placed right after the ground table (same authorities, now read against their own text) so the lanes argue FROM the rows.
    rebut = ""; rb = {}
    if os.environ.get("GP_REBUTTAL", "0") == "1":
        try:
            import rebuttal_table
            # GP_RECORD = the numbered pleading (complaint/affidavit) for the RECORD-CLOSURE axis — verified_record.md has no ¶ numbers;
            # GP_ORDER = the order under attack; the memo lives beside the pack so a rebuild costs only new cites.
            rec = os.environ.get("GP_RECORD") or a.verified
            rb = rebuttal_table.build(a.adverse, f"{d}/rebuttal_table.md", a.court, rec if os.path.exists(rec) else a.verified,
                                      os.environ.get("GP_ORDER") or None, memo_dir=f"{d}/.rebuttal_memo")
            rebut = open(f"{d}/rebuttal_table.md").read().split("\n", 1)[1]   # drop its H1; the pack has one
        except Exception as e:
            rebut = f"## REBUTTAL ROWS — build failed ({type(e).__name__}: {str(e)[:160]}); lanes fall back to the ground table\n"
    parts = [f"# GROUND PACK — {a.court} / {a.type} — built deterministically; bots reason on it, never re-fetch it\n",
             gt_md, rebut, gsec, forum, units, req, conn, face_md, vault, m0x]
    open(a.out, "w").write("\n".join(p for p in parts if p))
    print(json.dumps({"adverse_cites": gt["cites"], "resolved": gt["resolved"], "forum_rules": nrules, "forum_gaps": ngaps,
                      "face_rows": ft["rows"], "face_defects": ft["defects"], "rebuttal": rb, "connected_bytes": len(conn.encode()),
                      "bytes": os.path.getsize(a.out), "file": a.out}))


if __name__ == "__main__":
    main()
