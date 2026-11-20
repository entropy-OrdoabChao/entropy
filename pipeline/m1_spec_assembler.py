#!/usr/bin/env python3
"""m1_spec_assembler — F9 (2026-11-11): SPEC-DRIVEN assembler. The instrument's anatomy is an INPUT
(templates/filing_specs/<instrument>.yaml), not a Python lane. First spec: reply_to_new_matter (CLASS pleading).

Division of labour (user lock): deterministic = GUARDRAIL, LLM = comprehension.
  deterministic : enumerate the adverse paragraphs (every one gets a slot), quote each verbatim (quote fidelity),
                  pair each to OUR complaint paragraphs (recharacterization table / count map), pick the candidate
                  cells, enforce the grammar (kind ∈ allowed, Compl. ¶ cite present = departure gate, no general
                  denial, no argument headings) and the INSTRUMENT decision (sustained card CLASS == spec CLASS).
  LLM           : writes the response text for each paragraph and the preliminary-statement rule paragraphs FROM
                  the cells only. No cells for a paragraph -> CONCLUSION OF LAW / DENIED on the paired Compl. ¶,
                  flagged OPEN in the sidecar — never invented authority.
A paragraph whose response fails the grammar after one retry FAILS the section; the gate reports it. No
deterministic-only fallback draft: a draft that could not be written is not written.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, sqlite3, sys, urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:                       # pipeline python is /usr/bin/python3 (has yaml); keep the import error loud
    yaml = None

SPEC_DIR = Path(__file__).resolve().parent.parent / "templates" / "filing_specs"
if not SPEC_DIR.exists():   # a sandbox copy carries no templates/ — read the live tree's specs (never an empty dir that re-classes a pleading as a brief)
    for _cand in (Path(str(Path.home() / "beezle/templates/filing_specs")), Path.cwd() / "templates" / "filing_specs"):
        if _cand.exists():
            SPEC_DIR = _cand; break
CLASSES = ("pleading", "brief", "application", "affidavit", "original_proceeding")
GENERAL_DENIAL = re.compile(r"^\s*(?:denied\.?|denied as stated\.?|strict proof (?:is )?demanded\.?|plaintiff demands (?:strict )?proof[^.]*\.?)\s*$", re.I)
COMPL_CITE = re.compile(r"Compl(?:aint)?\.?\s*¶{1,2}\s*\d+", re.I)
HEADING = re.compile(r"^(?:COUNT\s+(?:[IVX]+|\d+)\b|NEW MATTER|AFFIRMATIVE DEFENSES?|ANSWER|WHEREFORE|VERIFICATION|CERTIFICATE OF SERVICE|PRELIMINARY STATEMENT|(?:THE )?PARTIES|(?:STATEMENT OF )?FACTS|JURISDICTION(?: AND VENUE)?|PRAYER FOR RELIEF|NOTICE TO (?:PLEAD|DEFEND))\b")
ARG_HEADING = re.compile(r"(?m)^#{1,3}\s*(?:ARGUMENT|[IVX]+\.\s+[A-Z])")


# ------------------------------------------------------------------ spec + decision guard
def load_spec(name_or_path):
    p = Path(name_or_path)
    if not p.exists():
        p = SPEC_DIR / f"{name_or_path}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no filing spec for '{name_or_path}' ({SPEC_DIR})")
    if yaml is None:
        raise RuntimeError("PyYAML missing for the pipeline interpreter")
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert spec.get("class") in CLASSES, f"spec {p.name}: class must be one of {CLASSES}"
    spec["_path"] = str(p)
    return spec


INSTRUMENT_RX = re.compile(r"INSTRUMENT:\s*([A-Za-z0-9_\-]+)\s*\|\s*CLASS:\s*(pleading|brief|application|affidavit|original_proceeding)", re.I)


def _decision_from_text(t, src):
    """one card text -> the first SUSTAINED instrument decision in it (or None)."""
    lines = t.splitlines()
    verd = {}
    for l in lines:                              # VERDICT: <lane> | <n> | SUSTAINED|OPEN | why
        if l.startswith("VERDICT:"):
            parts = [x.strip() for x in l.split("|")]
            if len(parts) >= 3:
                verd.setdefault(parts[0][8:].strip(), []).append(parts[2].upper())
    for l in lines:
        m = INSTRUMENT_RX.search(l)
        if m and l.startswith("CARD:"):
            lane = l.split("|")[0][5:].strip()
            sustained = ("SUSTAINED" in l.upper()) or any(v == "SUSTAINED" for v in verd.get(lane, []))
            if sustained:
                return {"instrument": m.group(1).lower(), "class": m.group(2).lower(), "line": l, "file": src}   # full line: m1 reads the rule cite from it (was l[:400] → 'NO RULE CITE ON THE CARD')
    return None


def instrument_decision(cards_paths, explicit=None):
    """The room's SUSTAINED instrument card: 'INSTRUMENT: <name> | CLASS: <class> ...' on a CARD line whose VERDICT is SUSTAINED.
    Returns {'instrument','class','line','file'} or None. Grading is the skeptic's (in-room); here we only READ the verdict.

    `explicit` (m1 --instrument, DESIGN LOCK 2026-11-11): a card FILE or the card LINE itself. It wins over the room glob
    and must still carry SUSTAINED — an explicit card is a pointer to a graded decision, never a licence to skip grading."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            txt, src = p.read_text(encoding="utf-8", errors="replace"), str(p)
        else:
            txt, src = str(explicit).strip(), "--instrument"
            if not txt.startswith("CARD:"):
                txt = "CARD: posture-vehicle | " + txt      # a bare card line passed on the command line
        return _decision_from_text(txt, src)
    for cp in cards_paths:
        d = _decision_from_text(Path(cp).read_text(encoding="utf-8", errors="replace"), str(cp))
        if d:
            return d
    return None


# ------------------------------------------------------------------ GROUND cards (room decides the enumerated ground per unit, 2026-11-14)
# The room's GROUND ROUND posts one card per unit: 'UNIT <n>: <title> — GROUND: <cite>(<k>) "<verbatim>" …' or '— GROUND: NONE → <where>'.
# m1 READS the SUSTAINED cards; it never chooses grounds itself (the LLM planner that did was a second comprehension layer and drifted).
# Unit key = the SAME key set unit_rx accepts below (UNIT/Issue/Unit/Point): the 2026-11-16 ground round posted 'Issue n:' cards the
# skeptic SUSTAINED 8/8 and this regex (UNIT only) read them as zero grounds → m1 refused to draft. One key set, both readers.
GROUND_RX = re.compile(r"(?:UNIT|Issue|Unit|Point)\s*#?\s*(\d{1,3})\s*[:.]\s*(.{0,600}?)\s*[—\-–|]+\s*GROUND:\s*(NONE|(?:Pa\.?\s*R\.?\s*C\.?\s*P\.?\s*)?1028\s*\(a\)\s*\((\d{1,2})\))", re.I)


def ground_decisions(cards_paths):
    """[{unit, title, ground ('1'..'n' or None), line, file}] from GROUND cards the skeptic SUSTAINED **for that unit**;
    one entry per unit (latest card wins). Verdict keys accepted: 'UNIT n' / 'Issue n' / 'Unit n' / the card's index in its lane."""
    out = {}
    unit_rx = re.compile(r"(?:UNIT|Issue|Unit|Point)\s*#?\s*(\d{1,3})\b", re.I)
    for cp in cards_paths:
        lines = Path(cp).read_text(encoding="utf-8", errors="replace").splitlines()
        sustained_units, sustained_idx, lane_cards = set(), set(), {}
        for l in lines:
            if l.startswith("CARD:"):
                lane = l.split("|")[0][5:].strip()
                lane_cards.setdefault(lane, []).append(l)
            elif l.startswith("VERDICT:"):
                parts = [x.strip() for x in l.split("|")]
                if len(parts) < 3 or parts[2].upper() != "SUSTAINED":
                    continue
                m = unit_rx.search(parts[1])
                if m: sustained_units.add(int(m.group(1)))
                elif parts[1].isdigit(): sustained_idx.add((parts[0][8:].strip(), int(parts[1])))
        for lane, cards in lane_cards.items():
            for i, l in enumerate(cards, 1):
                m = GROUND_RX.search(l)
                if not m:
                    continue
                u = int(m.group(1))
                if not (u in sustained_units or (lane, i) in sustained_idx or "SUSTAINED" in l.upper()):
                    continue
                g = None if m.group(3).upper() == "NONE" else m.group(4)
                title = re.split(r"\.\s|\s\|\s", m.group(2).strip(" —-–|"))[0][:120]
                out[u] = {"unit": u, "title": title, "ground": g, "line": l, "file": str(cp)}
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ name -> spec/class routing (F9, 2026-11-13)
# A filing NAME that has a declared anatomy must route to that anatomy. Before this, m1's stem router sent any name
# holding "reply"/"answer"/"response" to the BRIEF lane (REBUTTAL_STEMS), so a reply to New Matter — a PLEADING under
# Pa.R.C.P. 1017(a) — came out as a brief with argument headings whenever --spec was not passed by hand.
RESPONSIVE_PLEADING_MARKS = ("new_matter", "newmatter", "responsive_pleading",
                             "reply_to_counterclaim", "reply_to_cross_claim", "answer_to_complaint")
NOT_A_PLEADING_MARKS = ("brief", "memorandum", "memo_of_law")


def _norm_name(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def spec_stems():
    """every instrument that has a filing spec, longest name first (so the most specific spec wins a substring race)."""
    return sorted((p.stem for p in SPEC_DIR.glob("*.yaml")), key=len, reverse=True)


def spec_for_name(name):
    """the filing spec a filing NAME selects: exact stem match, else the longest spec stem contained in the name."""
    n = _norm_name(name)
    if not n:
        return None
    stems = spec_stems()
    if n in stems:
        return n
    return next((s for s in stems if s in n), None)


def is_responsive_pleading(name):
    """name-level CLASS test: a reply/answer to a pleading is a PLEADING (Pa.R.C.P. 1017(a)), never a brief.
    A name that also says "brief"/"memorandum" is what it says it is (an answering BRIEF is a brief)."""
    n = _norm_name(name)
    if any(b in n for b in NOT_A_PLEADING_MARKS):
        return False
    return any(m in n for m in RESPONSIVE_PLEADING_MARKS)


def class_guard(spec, decision):
    """Deterministic guardrail: m1 routes ONLY to a spec whose CLASS matches a SUSTAINED instrument decision."""
    if decision is None:
        return f"STOP: no SUSTAINED instrument card (INSTRUMENT: … | CLASS: …) in the room cards — the decision is graded before any draft"
    if decision["class"] != spec["class"]:
        return f"STOP: instrument class mismatch — room decided CLASS {decision['class']} ({decision['instrument']}); spec {spec['instrument']} is CLASS {spec['class']}"
    return None


# ------------------------------------------------------------------ deterministic inputs
def numbered_paragraphs(text, start_heading=None, stop_headings=("WHEREFORE", "VERIFICATION", "CERTIFICATE OF SERVICE")):
    """{n: text} for 'n.' paragraphs; optionally only after a heading (NEW MATTER) and before the stop headings."""
    t = text
    if start_heading:
        m = re.search(rf"(?im)^\s*{re.escape(start_heading)}\s*$", t)
        if m:
            t = t[m.end():]
    stop = re.search(r"(?im)^\s*(?:%s)\b" % "|".join(re.escape(s) for s in stop_headings), t)
    if stop:
        t = t[:stop.start()]
    out, cur, buf = {}, None, []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,3})\.\s+(\S.*)$", line)
        if m and (cur is None or int(m.group(1)) == cur + 1 or int(m.group(1)) > cur):
            if cur is not None:
                out[cur] = " ".join(b for b in buf if b != "\x00").strip()
            cur, buf = int(m.group(1)), [m.group(2)]
        elif cur is not None and line.strip():
            st = line.strip()
            if HEADING.match(st) and st == st.upper():
                buf.append("\x00")              # heading line: closes the paragraph text (never part of an averment)
                continue
            if buf and buf[-1] == "\x00":
                continue                         # text after a heading and before the next number is not this ¶ either
            buf.append(st)
    if cur is not None:
        out[cur] = " ".join(b for b in buf if b != "\x00").strip()
    if not out and not start_heading:
        # S-unnumbered-paper-is-¶1 (2026-11-16): a paper with no 'n.' paragraphs (Progressive's one-block Response to the motion to
        # vacate) is read the way the court and the room read it — its body is ¶1. The room's cards, verified_record.md and the
        # ADVERSE_POINTS all key that paper as "¶1 clause (i)/(ii)"; a reader returning {} here stopped the profile lane on a GREEN
        # room ("no numbered paragraphs in the adverse paper"). Body = after the intro clause ("… as follows:") or the title block,
        # up to the signature block / stop heading; PDF line-wrap blanks are joined.
        body = t
        m = re.search(r"(?is)\bas follows\s*:\s*\n", body)
        if m:
            body = body[m.end():]
        else:
            lines = body.splitlines()
            k = next((i for i in range(len(lines) - 1, -1, -1) if HEADING.match(lines[i].strip()) and lines[i].strip() == lines[i].strip().upper()
                      and len(lines[i].strip()) > 12), None)
            if k is not None:
                body = "\n".join(lines[k + 1:])
        end = re.search(r"(?im)^\s*(?:Respectfully submitted|/s/|By:\s*/s/|Dated?:)", body)
        if end:
            body = body[:end.start()]
        body = " ".join(s.strip() for s in body.splitlines() if s.strip())
        if len(body) > 40:
            out[1] = body
    return out


def count_map(complaint_text):
    """COUNT heading -> (first ¶, last ¶) from OUR complaint; the deterministic pairing fallback."""
    paras = numbered_paragraphs(complaint_text)
    heads = [(m.start(), m.group(0).strip()) for m in re.finditer(r"(?im)^\s*COUNT\s+([IVX]+|\d+)\b.*$", complaint_text)]
    out, prev_max = {}, 0
    for i, (pos, h) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(complaint_text)
        seg = complaint_text[pos:end]
        stop = re.search(r"(?im)^\s*(?:VERIFICATION|CERTIFICATE OF SERVICE|NOTICE TO DEFEND|EXHIBIT\s+[A-Z0-9]+)\s*$", seg)
        if stop:
            seg = seg[:stop.start()]                      # the last count's segment otherwise runs into the verification / exhibits
        nums = [int(x) for x in re.findall(r"(?m)^\s*(\d{1,3})\.\s", seg)]
        nums = [n for n in nums if n > prev_max]           # paragraph numbers only ascend through the complaint
        cm = re.search(r"COUNT\s+([IVX]+|\d+)", h, re.I)
        if nums and cm:
            out[cm.group(1).upper()] = (min(nums), max(nums)); prev_max = max(nums)
    return paras, out


def pairing_from_table(table_path):
    """recharacterization_table.md rows: 'THEY SAY ¶n' ... 'WE PLEADED ¶m' -> {n: [m,...]}"""
    out = {}
    if not table_path or not Path(table_path).exists():
        return out
    t = Path(table_path).read_text(encoding="utf-8", errors="replace")
    for row in re.split(r"(?m)^## R\d+", t)[1:]:
        a = re.search(r"THEY SAY[^¶]*¶\s*(\d+)", row)
        ours = [int(x) for x in re.findall(r"WE PLEADED[^¶]*¶{1,2}\s*(\d+)", row)] + [int(x) for x in re.findall(r"Compl\.?\s*¶{1,2}\s*(\d+)", row)]
        if a and ours:
            out.setdefault(int(a.group(1)), []).extend(sorted(set(ours)))
    return out


def pair_paragraph(n, their_text, table_pairs, counts, our_paras=None, k=3):
    ours = list(table_pairs.get(n, []))
    if our_paras and not ours:                      # no table row / no count named: top-k OUR paragraphs by token overlap
        q = _tok(their_text)
        sc = sorted(((len(q & _tok(t)), m) for m, t in our_paras.items()), key=lambda x: -x[0])
        ours += [m for s_, m in sc[:k] if s_ >= 2]
    for r in re.findall(r"\bCounts?\s+((?:[IVX]+|\d+)(?:\s*(?:,|and|&|through|-|–)\s*(?:[IVX]+|\d+))*)", their_text, re.I):
        for c in re.findall(r"[IVX]+|\d+", r):
            if c.upper() in counts:
                lo, hi = counts[c.upper()]
                ours.extend([lo, hi])
    return sorted(set(ours))


def load_cells(paths):
    """gated room cards (CARD: lines) and/or cells.jsonl -> list of strings (one per cell)."""
    cells = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        if p.suffix == ".jsonl":
            for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(l); cells.append(d.get("text") or d.get("cell") or json.dumps(d, ensure_ascii=False))
                except Exception:
                    pass
        else:
            cells += [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                      if l.startswith("CARD:") and "FAILED (" not in l and "SILENT (" not in l and "| OPEN | PROPOSITION: none" not in l]
    return cells


DB = (_eroot() / "corpus/chunk_text.db")
RAW_RCP = (_eroot() / "corpus_intake/pa_rcp/chapters")
RULE_SOURCES = {"Pa.R.C.P.": lambda n: f"pa_code_t231_ch{(int(n) // 1000) * 1000 if int(n) >= 1000 else (int(n) // 100) * 100}",
                "Pa.R.A.P.": lambda n: f"pa_code_t210_ch{int(n) // 100}"}        # Pa.R.A.P. 341 lives in Chapter 3


def rule_texts(cites):
    """VERBATIM rule text for each cite in spec.rule_basis, resolved by SOURCE KEY (chunk_text.source + citations json), never by
    LIKE on chunk_text. Returns {cite: {'text','chunks','source'}}; a cite with no text is reported as NOT IN CORPUS — the
    preliminary statement may only quote what is here."""
    out = {}
    con = sqlite3.connect(DB) if DB.exists() else None
    for cite in cites:
        # Phila.Civ.R. *1028(c) → source range phila_cr_1028… (ingest key = rule number + title slug); range scan uses the
        # source index — never LIKE on text (S-filing-type-on-demand 2026-11-14: the local rule is what makes a PO packet)
        lm = re.match(r"Phila\.?\s*Civ\.?\s*R\.?\s*\*?([\d.]+(?:\([a-z0-9]\))*)", cite)
        if lm and con is not None:
            num = re.sub(r"[^0-9a-z]", "", lm.group(1).lower()); got, ids = [], []   # *205.2(b) -> 2052b (the ingest key)
            try:
                for cid, txt in con.execute("SELECT chunk_id, text FROM chunk_text WHERE source >= ? AND source < ? ORDER BY chunk_id",
                                            (f"phila_cr_{num}", f"phila_cr_{num}~")):
                    if txt: got.append(txt); ids.append(cid)
            except sqlite3.Error:
                pass
            out[cite] = {"text": "\n".join(got), "chunks": ids, "source": f"phila_cr_{num}*"} if got else None
            continue
        m = re.match(r"(Pa\.R\.C\.P\.|Pa\.R\.A\.P\.)\s*(\d{3,4})", cite)
        if not m:
            out[cite] = None; continue
        fam, num = m.group(1), m.group(2)
        src = RULE_SOURCES[fam](num); got, ids = [], []
        if con is not None:
            try:
                for cid, txt, cj in con.execute("SELECT chunk_id, text, citations FROM chunk_text WHERE source=?", (src,)):
                    if f"{fam} {num}" in (cj or "") and txt:
                        got.append(txt); ids.append(cid)
            except sqlite3.Error:
                pass
        if not got and fam == "Pa.R.C.P.":                 # raw per-rule pull (ingest_pa_rcp_chapter) — same official page
            raw = RAW_RCP / str((int(num) // 1000) * 1000 if int(num) >= 1000 else (int(num) // 100) * 100) / f"rule_{num}.txt"
            if raw.exists():
                got = [raw.read_text(encoding="utf-8", errors="replace")]; ids = [f"raw:{raw.name}"]
        out[cite] = {"text": "\n".join(got), "chunks": ids, "source": src} if got else None
    if con is not None:
        con.close()
    return out


def _norm(s):
    return re.sub(r"[\s“”\"'’‘]+", " ", s).strip().lower()


def quote_fidelity(paragraph, corpora, min_words=6):
    """every “quoted span” in the paragraph must appear verbatim (whitespace/quote-normalised) in one of the corpora texts."""
    spans = re.findall(r"[“\"]([^”\"]{12,}?)[”\"]", paragraph)
    if not spans:
        return [f"no verbatim quotation (≥{min_words} words in quotation marks)"]
    hay = _norm("\n".join(corpora))
    bad = [sp for sp in spans if len(sp.split()) >= min_words and _norm(sp) not in hay]
    return [f"quote not verbatim in any provided rule/cell: “{sp[:80]}…”" for sp in bad]


STOP = set("the a an of to in and or for by on at is are be was were with that this as from it its their our any all not no".split())
def _tok(s):
    return {w for w in re.findall(r"[a-z0-9§¶]+", s.lower()) if w not in STOP and len(w) > 2}


_FIG_RX = re.compile(r"\d[\d,]*\.\d\d|\b\d{5,7}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}\b")
_BOILER = _tok("denied specifically admitted writing speaks itself characterization inconsistent terms further response defendant plaintiff averment")


def _pleads(resp_text, para_text):
    """does OUR paragraph plead the fact the response states? shared figure/date/doc-number, or >=3 shared content tokens"""
    if set(_FIG_RX.findall(resp_text)) & set(_FIG_RX.findall(para_text)): return True
    return len((_tok(resp_text) - _BOILER) & _tok(para_text)) >= 3


def _cite_check(text, paired, our_paras, counts, kind=None):
    """guardrail: every 'Compl. ¶n' must be a real paragraph of OUR complaint and must PLEAD the fact the response relies on, or (for a
    conclusion-of-law response / a response stating no figure of its own) sit in the paired set or the paired count's range.
    S-cite-pleads-the-fact (class, 2026-11-13): the old range-only test accepted a definition ¶ inside Count I and rejected the facts
    ¶ (¶18, '$161.36 refund due') outside it — the drafter learned to cite the wrong paragraph four runs in a row."""
    out = []
    cited = [int(x) for x in re.findall(r"Compl(?:aint)?\.?\s*¶{1,2}\s*(\d+)", text, re.I)]
    for c in cited:
        if c not in our_paras:
            out.append(f"Compl. ¶{c} does not exist"); continue
        if _pleads(text, our_paras[c]):
            continue
        if paired:
            in_range = c in paired or any(lo <= c <= hi for (lo, hi) in counts.values() if any(lo <= p <= hi for p in paired))
            if not in_range:
                out.append(f"Compl. ¶{c} outside the paired paragraphs {paired} and does not plead the fact stated")
            elif kind in ("DENIED", "WRITING") and _FIG_RX.search(text):
                out.append(f"Compl. ¶{c} does not plead the figure/date/document the response relies on — cite the Complaint paragraph that does")
    return out


# an averment of the pleader's own conduct (actor + conduct verb) is a FACT averment, never a conclusion of law — guardrail for the LLM classification
CONDUCT_RX = re.compile(r"\b(acted|performed (?:all|its|each)|has (?:taken|made) no (?:action|statement|representation)|did not (?:engage|refuse|act|fail|breach)|took no action|complied with)\b", re.I)


try:
    _REDRAFT = json.loads(os.environ.get("M1_REDRAFT", "") or "{}")
except Exception:
    _REDRAFT = {}


def candidates(their_text, ours_text, cells, k=6):
    q = _tok(their_text) | _tok(ours_text)
    scored = sorted(((len(q & _tok(c)), c) for c in cells), key=lambda x: -x[0])
    return [c for s, c in scored[:k] if s >= 2]


# ------------------------------------------------------------------ LLM (comprehension)
def _model(llm_url):
    try:
        d = json.loads(urllib.request.urlopen(llm_url.rstrip("/") + "/models", timeout=20).read())
        return d["data"][0]["id"]
    except Exception:
        return "default"


def llm_json(llm_url, system, user, max_tokens=3500, temperature=0.1):
    body = {"model": _model(llm_url), "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},            # Qwen3: reasoning off — the budget is for the JSON
            "messages": [{"role": "system", "content": system + " /no_think"}, {"role": "user", "content": user}]}
    req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    txt = json.loads(urllib.request.urlopen(req, timeout=600).read())["choices"][0]["message"]["content"]
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    txt = re.sub(r"```(?:json)?", "", txt)
    v = _extract_json(txt)
    if v is None:
        v = _salvage_truncated(txt)
    return v


def _salvage_truncated(txt):
    """S-truncated-json (po-16 run 7: brief section returned None ×3 — the model hit max_tokens mid-string, so raw_decode found no
    well-formed value and a 20-authority brief was thrown away). A section answer is {"<key>": "<long text>"}: recover the string
    value of the first key as far as it got (unescape \\n, \\"), mark it truncated. Caller's guards (verbatim quotes, required
    authorities) still judge it; nothing is invented."""
    m = re.search(r"\{\s*\"([^\"]{1,80})\"\s*:\s*\"", txt)
    if not m: return None
    body = txt[m.end():]
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]; out.append({"n": "\n", "t": "\t", "\"": "\"", "\\": "\\", "/": "/"}.get(nxt, nxt)); i += 2; continue
        if ch == "\"": break
        out.append(ch); i += 1
    val = "".join(out).strip()
    if len(val) < 400: return None
    return {m.group(1): val, "__truncated__": True}


def _extract_json(txt):
    """first well-formed JSON value in free text (raw_decode scan); several bare objects -> list of them."""
    dec = json.JSONDecoder(); vals = []
    i = 0
    while i < len(txt):
        j = min([k for k in (txt.find("[", i), txt.find("{", i)) if k >= 0], default=-1)
        if j < 0:
            break
        try:
            v, end = dec.raw_decode(txt, j); vals.append(v); i = end
        except json.JSONDecodeError:
            i = j + 1
    if not vals:
        return None
    dict_lists = [v for v in vals if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    if dict_lists:
        return dict_lists[-1]                      # the answer comes LAST; earlier lists are reasoning debris like [108, 109]
    str_lists = [v for v in vals if isinstance(v, list) and v and all(isinstance(x, str) for x in v)]
    if str_lists:
        return str_lists[-1]
    dicts = [v for v in vals if isinstance(v, dict)]
    if dicts:
        return dicts if len(dicts) > 1 else dicts[0]
    return None


SYS_RESP = ("You draft numbered RESPONSES in a Pennsylvania REPLY TO NEW MATTER for a plaintiff appearing sui juris. "
            "You are bound by the DEPARTURE rule: a reply must support the complaint as pleaded (41 Am Jur Pleading § 185) — never argue, never "
            "restate a claim in the defendant's vocabulary, never add a new claim. For each adverse paragraph return exactly one response of kind "
            "ADMITTED (record facts only), DENIED (specific denial: state the verified fact that contradicts, cite the complaint paragraph that pleads it) or "
            "CONCLUSION_OF_LAW (their paragraph states LAW, a recital of a court order, or a legal characterization — 'no response required; deemed denied, "
            "Pa.R.C.P. 1029(d); Plaintiff stands on Compl. ¶…'), or WRITING (their paragraph quotes or characterizes a DOCUMENT — a policy, contract, "
            "notice, letter or exhibit: 'The writing speaks for itself; any characterization inconsistent with its terms is denied. By way of further "
            "response, <the RECORD FACT from the CELLS that the writing, applied to the record, contradicts — prefer facts drawn from the defendant's OWN "
            "exhibits>, Compl. ¶n.' — a quoted document is never a conclusion of law). CONDUCT RULE (Pa.R.C.P. 1029(b)/(d)): an averment about the pleader's "
            "OWN CONDUCT — 'acted in good faith', 'acted reasonably/prudently/lawfully', 'performed all duties', 'did not engage in', 'has taken no action', "
            "'did not refuse' — is an averment of FACT, never CONCLUSION_OF_LAW (a deemed denial there is an admission); it is DENIED with the record fact "
            "from the CELLS that contradicts THAT conduct — the fact must answer the averment (a good-faith averment is met by the defendant's "
            "contradictory statements/retroactive documents or an acknowledged-and-unpaid sum, not by an unrelated fact); prefer the defendant's own "
            "admissions and exhibits. AMOUNTS: a sum not written in the Complaint may be stated only when the Complaint pleads the DEMAND for that money "
            "(all post-sale premiums, the declared refund) — then cite the Complaint paragraph pleading the demand and attribute the figure to its source "
            "('per Defendant's own draw records / Defendant's Answer ¶n'); never present a figure as if the Complaint pleaded it. CITE RULE: a DENIED or "
            "WRITING response cites the Complaint paragraph that PLEADS the fact it states (the paragraph containing that date, sum or document); a "
            "CONCLUSION_OF_LAW response cites the count/claim paragraph the legal averment attacks. Every response MUST cite at least one 'Compl. ¶n' from the paired "
            "paragraphs given. Use ONLY the facts and authorities in the CELLS provided (a 'RECORD FACT' cell is a verified record fact you may plead); if no "
            "cell supports a fact, do not assert it. Never write a bare 'Denied.' "
            "Output JSON list: [{\"n\": <adverse ¶>, \"kind\": \"ADMITTED|DENIED|CONCLUSION_OF_LAW|WRITING\", \"text\": \"<1-4 sentences>\"}]. No prose outside JSON.")

SYS_PRELIM = ("You write the PRELIMINARY STATEMENT of a Pennsylvania reply to new matter: at most 8 paragraphs, each stating ONE rule as a VERBATIM "
              "quotation in quotation marks (copy the words exactly from the RULE TEXT or CELLS provided — at least 6 consecutive words, no paraphrase) "
              "followed by its citation, then one sentence applying it to the record fact given. A rule marked NOT available may not be quoted or cited. "
              "Zero adjectives, zero argument, no case narrative. Output a JSON list of strings (one per paragraph), nothing else.")


# ------------------------------------------------------------------ assembly
class _Safe(dict):
    def __missing__(self, k): return "{" + k + "}"

def _fill(t, ctx):
    try: return str(t).format_map(_Safe(ctx))
    except Exception: return str(t)

def _record_ctx(case_id, nm):
    """Record-derived values for spec fact templates. Deterministic: env COUNTS (chain's surviving counts, e.g. 'I,IV') →
    'Counts I and IV remain'; CASE_TIMELINE.json first 'ORDER ENTERED' event → order_date; nm = adverse ¶ numbers."""
    import os, json
    cs = [c.strip().upper() for c in os.environ.get("COUNTS", "").split(",") if c.strip()]
    if len(cs) == 0: surv = "the surviving counts remain"
    elif len(cs) == 1: surv = f"Count {cs[0]} remains"
    else: surv = "Counts " + ", ".join(cs[:-1]) + f" and {cs[-1]} remain"
    od = "the order"
    _surv_rec = None
    try:
        from pathlib import Path as _P
        slug = str(case_id).split(":")[-1]
        for cand in list(_P("obsidian_sync/cases").glob(f"*/CASE_TIMELINE.json")):
            _head = cand.read_text(errors="ignore")[:6000]
            if slug in str(cand) or str(case_id) in str(cand) or case_id in _head or (slug and slug in _head):
                _j = json.loads(cand.read_text(errors="ignore"))
                _evs = _j if isinstance(_j, list) else (_j.get("timeline") or _j.get("events") or [])
                for ev in _evs:
                    txt = json.dumps(ev)
                    if re.search(r"^\W*ORDER ENTERED", str(ev.get("event", "")), re.I) and re.search(r"DISMISS|SUSTAIN", txt, re.I):
                        d = re.search(r"DOCKETED (\d{4}-\d{2}-\d{2})", txt) or re.search(r"\d{4}-\d{2}-\d{2}", txt)
                        d = re.search(r"(\d{4}-\d{2}-\d{2})", d.group(0)) if d else None
                        _m = re.search(r"SURVIVING: (.*?)(?:\. Case remains|\.\\\"|$)", txt)
                        if _m:
                            _cs = re.findall(r"Count ([IVX]+)", _m.group(1))
                            if _cs: _surv_rec = _cs
                        if d:
                            import datetime as _dt
                            od = "the order entered " + _dt.date.fromisoformat(d.group(1)).strftime("%B %d, %Y").replace(" 0", " ")
                            raise StopIteration
    except StopIteration: pass
    except Exception: pass
    if _surv_rec:
        surv = f"Count {_surv_rec[0]} remains" if len(_surv_rec) == 1 else "Counts " + ", ".join(_surv_rec[:-1]) + f" and {_surv_rec[-1]} remain"
    nm = sorted(nm) if nm else []
    return {"surviving_counts": surv, "order_date": od, "nm_first": nm[0] if nm else "", "nm_last": nm[-1] if nm else "", "nm_count": len(nm)}


def assemble(spec, adverse_path, complaint_path, cells_paths, out_path, case_id, court, llm_url,
             pairing_path=None, face_path=None, cards_paths=None, decision=None):
    adverse = Path(adverse_path).read_text(encoding="utf-8", errors="replace")
    complaint = Path(complaint_path).read_text(encoding="utf-8", errors="replace")
    our_paras, counts = count_map(complaint)
    nm = numbered_paragraphs(adverse, start_heading="NEW MATTER")
    if not nm:
        nm = numbered_paragraphs(adverse)
    if not nm:
        return {"ok": False, "stop": "no numbered adverse paragraphs found"}
    pairs_tbl = pairing_from_table(pairing_path)
    cells = load_cells(cells_paths)
    face = Path(face_path).read_text(encoding="utf-8", errors="replace") if face_path and Path(face_path).exists() else ""
    sect = {s["id"]: s for s in spec["sections"]}
    allowed = set(sect["responses"].get("allowed", ["ADMITTED", "DENIED_SPECIFICALLY", "CONCLUSION_OF_LAW", "WRITING"]))
    allowed = {a.replace("_SPECIFICALLY", "") for a in allowed}
    side = {"instrument": spec["instrument"], "class": spec["class"], "decision": decision, "paragraphs": {}, "open": [], "fail": []}

    # --- responses: deterministic slots, LLM text, grammar enforced
    items = []
    for n in sorted(nm):
        ours = pair_paragraph(n, nm[n], pairs_tbl, counts, our_paras)
        ours_txt = "\n".join(f"Compl. ¶{m}: {our_paras.get(m, '')[:400]}" for m in ours if m in our_paras)[:2400]
        cands = candidates(nm[n], ours_txt, cells)
        # S-cite-pleads-the-fact (class, 2026-11-13): the response must cite the Complaint ¶ that PLEADS the fact it relies on, not merely a
        # paired ¶ (¶117 relied on Answer ¶14 but cited Compl. ¶96; ¶123's dates sit in Compl. ¶24/¶28/¶35, it cited ¶2). Offer, beside the
        # paired ¶¶, the top-k OUR paragraphs that share the candidate cells' facts (figures/dates/document words), and let _cite_check accept them.
        support = []
        if cands:
            q = set().union(*[_tok(c) for c in cands]) - _tok(nm[n])
            # figures/dates/doc numbers in the cells are the strongest cite signal: a Complaint ¶ carrying the same '161.36' or '106621'
            # is the paragraph that PLEADS the fact (token overlap alone ranked a definition ¶ above it)
            figs = set(re.findall(r"\d[\d,]*\.\d\d|\b\d{5,7}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", " ".join(cands)))
            def _score(t):
                return len(q & _tok(t)) + 5 * sum(1 for f in figs if f in t)
            sc = sorted(((_score(t), m) for m, t in our_paras.items() if m not in ours), key=lambda x: -x[0])
            support = [m for s_, m in sc[:6] if s_ >= 2]
        if support:
            ours_txt = (ours_txt + "\n" + "\n".join(f"Compl. ¶{m}: {our_paras.get(m, '')[:400]}" for m in support))[:3600]
        # S-review-loop (class): the draft skeptic (scripts/rooms/draft_review.py) REJECTS a ¶ → the chain re-runs m1 with
        # M1_REDRAFT='{"116": "<why>", ...}'; for those ¶¶ the pool widens to every RECORD FACT cell + top-12 and the verdict is pasted
        # so comprehension fixes it (a non sequitur denial is a wrong CELL choice, not a wrong rule). Bounded: one loop per chain run.
        redraft = _REDRAFT.get(str(n))
        if redraft:
            rf = [c for c in cells if c.startswith("RECORD FACT") and c not in cands]
            cands = (cands + rf + candidates(nm[n], ours_txt, cells, k=12))[:16]
            cands = list(dict.fromkeys(cands))
            nm_note = f"\nSKEPTIC REJECTED THE PREVIOUS RESPONSE TO ¶{n}: {redraft} — choose a different cell whose fact ANSWERS this averment."
        else:
            nm_note = ""
        items.append({"n": n, "their": nm[n] + nm_note, "ours": ours + support, "ours_txt": ours_txt, "cells": cands})
    responses = {}
    B = 3
    for i in range(0, len(items), B):
        batch = items[i:i + B]
        user = "\n\n".join(
            f"ADVERSE ¶{it['n']}: {it['their'][:900]}\nPAIRED COMPLAINT PARAGRAPHS:\n{it['ours_txt'] or '(none paired — treat as CONCLUSION_OF_LAW standing on the Complaint as a whole; cite the count paragraphs if the count is named)'}\nCELLS:\n" +
            ("\n".join("- " + c[:500] for c in it["cells"]) or "- (none)") for it in batch)
        got = None
        for attempt in (1, 2):
            try:
                got = llm_json(llm_url, SYS_RESP, user)
            except Exception as e:
                side["fail"].append(f"batch {i//B+1} llm error: {e}")
            if isinstance(got, list):
                break
        for it in batch:
            r = next((g for g in (got or []) if isinstance(g, dict) and str(g.get("n")) == str(it["n"])), None)
            kind = (r or {}).get("kind", "").upper().replace(" ", "_")
            text = ((r or {}).get("text") or "").strip()
            if kind == "DENIED_SPECIFICALLY":
                kind = "DENIED"
            bad = []
            if kind not in allowed: bad.append(f"kind '{kind}' not allowed")
            if kind == "CONCLUSION_OF_LAW" and CONDUCT_RX.search(it["their"]): bad.append("averment of the pleader's own conduct is FACT (1029(b)) — deny with a record fact, not deemed denied")
            if not COMPL_CITE.search(text): bad.append("no Compl. ¶ cite (departure gate)")
            if GENERAL_DENIAL.match(text): bad.append("general denial = admission (1029(b))")
            bad += _cite_check(text, it["ours"], our_paras, counts, kind)
            if bad:
                # one retry for this paragraph alone
                try:
                    g2 = llm_json(llm_url, SYS_RESP, f"Your previous answer for ¶{it['n']} failed: {'; '.join(bad)}. Fix it. Cite ONLY these complaint paragraphs: {it['ours'] or 'any that exists in the paired list'}. Return a JSON list with ONE object.\n\nADVERSE ¶{it['n']}: {it['their'][:900]}\nPAIRED COMPLAINT PARAGRAPHS:\n{it['ours_txt'] or '(none)'}\nCELLS:\n" + ("\n".join('- ' + c[:500] for c in it['cells']) or '- (none)'))
                    r2 = g2[0] if isinstance(g2, list) and g2 else (g2 if isinstance(g2, dict) else None)
                    if r2:
                        kind = str(r2.get("kind", "")).upper().replace("_SPECIFICALLY", "").replace(" ", "_"); text = (r2.get("text") or "").strip()
                        bad = [b for b in [("kind not allowed" if kind not in allowed else ""), ("no Compl. ¶ cite" if not COMPL_CITE.search(text) else ""), ("general denial" if GENERAL_DENIAL.match(text) else "")] if b] + _cite_check(text, it["ours"], our_paras, counts, kind)
                except Exception as e:
                    bad.append(f"retry llm error: {e}")
            if bad:
                side["fail"].append(f"¶{it['n']}: {'; '.join(bad)}")
                continue
            if not it["cells"]:
                side["open"].append(f"¶{it['n']}: no cell supported the response — review")
            responses[it["n"]] = (kind, text)
            side["paragraphs"][it["n"]] = {"kind": kind, "paired": it["ours"], "cells": len(it["cells"])}

    # --- preliminary statement: VERBATIM rule text (by source key) + rule cells in; quote-fidelity gate out
    rules = rule_texts(spec.get("rule_basis", []))
    missing_rules = [c for c, v in rules.items() if not v]
    side["rules_not_in_corpus"] = missing_rules
    rule_block = "\n\n".join(f"[{c}] [corpus:{v['source']}]\n{v['text'][:2200]}" for c, v in rules.items() if v)
    rule_cells = [c for c in cells if re.search(r"Pa\.R\.C\.P\.|Pa\.R\.A\.P\.|Am Jur|1029|1026|1361|1024|1030|341|departure", c)]
    face_rows = [l for l in face.splitlines() if any(k in l for k in ("notice to plead", "verification", "response posture", "paper kind"))]
    must = sect["preliminary_statement"].get("must_cover", [])
    topics = [m if isinstance(m, dict) else {"id": m} for m in must]
    # S-spec-facts-from-record (2026-11-13): a CLASS spec may not carry case values ("Count I remains", "October 21, 2026").
    # must_cover[].fact is a template; {surviving_counts} {order_date} {nm_first} {nm_last} {nm_count} are filled from the record
    # (COUNTS env = the chain's surviving-count selection; CASE_TIMELINE 'ORDER ENTERED' date; the adverse paper's own ¶ range).
    _ctx = _record_ctx(case_id, nm)
    topics = [dict(tp, fact=_fill(tp.get("fact", ""), _ctx)) if tp.get("fact") else tp for tp in topics]
    topic_block = "\n".join(f"{i+1}. [{tp.get('id')}] QUOTE FROM: {tp.get('rule', 'any rule below')} — APPLY TO THIS FACT: {tp.get('fact', '(from the face table)')}" for i, tp in enumerate(topics))
    corpora = [v["text"] for v in rules.values() if v] + rule_cells
    prelim, pf = [], []
    for attempt in (1, 2):
        try:
            got = llm_json(llm_url, SYS_PRELIM,
                           (f"PREVIOUS ATTEMPT FAILED: {pf}. Quote ONLY words that appear in the RULE TEXT below, inside quotation marks.\n\n" if pf else "") +
                           f"WRITE EXACTLY {len(topics)} PARAGRAPHS, one per topic, in this order — each = verbatim quotation from the named rule + citation + one sentence applying it to the stated fact (use the fact as given; add nothing):\n{topic_block}\n\nRECORD FACTS (face table of the adverse paper):\n" + "\n".join(face_rows[:8]) +
                           f"\n\nRULE TEXT (verbatim, official; cite as shown — rules NOT available: {missing_rules or 'none'}; do not quote or cite an unavailable rule):\n{rule_block}" +
                           "\n\nCELLS (graded room cards; quotable):\n" + "\n".join("- " + c[:600] for c in rule_cells[:20]) +
                           f"\n\nCASE: {case_id}; court {court}. The adverse paper has {len(nm)} New Matter paragraphs ¶{min(nm)}–¶{max(nm)}.")
        except Exception as e:
            side["fail"].append(f"preliminary statement llm error: {e}"); got = None
        if isinstance(got, list) and got and all(isinstance(x, str) for x in got):
            pf = [f"¶{i+1}: {'; '.join(quote_fidelity(x, corpora))}" for i, x in enumerate(got) if quote_fidelity(x, corpora)]
            for i, tp in enumerate(topics):                      # coverage: the i-th paragraph must carry its rule cite and a topic keyword
                para = got[i] if i < len(got) else ""
                rnum = re.search(r"\d{3,4}", tp.get("rule", "") or "")
                if tp.get("rule") and (not rnum or rnum.group(0) not in para):
                    pf.append(f"¶{i+1} [{tp['id']}]: does not cite {tp['rule']}")
                kws = tp.get("keywords") or []
                if kws and not any(k.lower() in para.lower() for k in kws):
                    pf.append(f"¶{i+1} [{tp['id']}]: topic not covered (none of {kws})")
                if tp.get("rule") and not rules.get(tp["rule"].split("(")[0].strip()):
                    pf.append(f"¶{i+1} [{tp['id']}]: rule {tp['rule']} NOT IN CORPUS — cannot be quoted")
            prelim = got
            if not pf:
                break
        else:
            pf = ["not a JSON list of strings"]
    if pf:
        side["fail"].append("preliminary statement quote-fidelity: " + " | ".join(pf)[:600])
    if not prelim:
        side["fail"].append("preliminary statement not produced")

    # --- render (finisher adds caption / verification / COS for the forum)
    out = [f"## {spec['title']}", "", "## PRELIMINARY STATEMENT", ""]
    out += [f"{i+1}. {p.strip()}" for i, p in enumerate(prelim)]
    out += ["", "## REPLY TO NEW MATTER", "",
            f"Plaintiff replies to Defendant's New Matter (¶¶ {min(nm)}–{max(nm)}) paragraph by paragraph. A response of \"conclusion of law\" is made to an averment that states law, recites an order of court, or characterizes the Complaint; no response is required to such averments and they are deemed denied (Pa.R.C.P. 1029(d)). Each response stands on the Complaint as pleaded.", ""]
    for n in sorted(nm):
        quote = nm[n][:240].rstrip() + ("…" if len(nm[n]) > 240 else "")
        if n in responses:
            kind, text = responses[n]
            label = {"ADMITTED": "ADMITTED", "DENIED": "DENIED — specifically", "CONCLUSION_OF_LAW": "CONCLUSION OF LAW — no response required; deemed denied",
                     "WRITING": "WRITING — speaks for itself; characterization denied"}[kind]
            out.append(f"{n}. (Defendant's ¶{n}: \"{quote}\")  {label}. {text}")
        else:
            out.append(f"{n}. (Defendant's ¶{n}: \"{quote}\")  [RESPONSE NOT WRITTEN — gate FAIL]")
        out.append("")
    out += ["", sect["wherefore"]["text"], ""]
    md = "\n".join(out)
    if ARG_HEADING.search(md):
        side["fail"].append("argument heading present (departure)")
    Path(out_path).write_text(md, encoding="utf-8")
    Path(out_path).with_suffix(".spec_sidecar.json").write_text(json.dumps(side, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"ok": not side["fail"], "paragraphs": len(nm), "answered": len(responses), "open": len(side["open"]),
            "fail": side["fail"], "out": str(out_path)}


# ------------------------------------------------------------------ gate (deterministic; called by m1_filing_gate for spec types)
def spec_gate(draft_text, spec, adverse_text=None):
    f = []
    g = spec.get("gate", {})
    low = draft_text.lower()
    for part in g.get("mandatory", []):
        key = {"caption": "court of common pleas", "control_no": "control no", "preliminary_statement": "preliminary statement",
               "responses": "reply to new matter", "wherefore": "wherefore", "verification": "verification",
               "certificate_of_service": "certificate of service"}.get(part, part.replace("_", " "))
        if key not in low:
            f.append(f"MISSING {part.upper()}")
    if "[response not written" in low:
        f.append("UNANSWERED PARAGRAPH(S): " + str(low.count("[response not written")))
    if g.get("every_adverse_paragraph_answered") and adverse_text:
        nm = numbered_paragraphs(adverse_text, start_heading="NEW MATTER") or numbered_paragraphs(adverse_text)
        missing = [n for n in nm if not re.search(rf"(?m)^{n}\.\s+\(Defendant's ¶{n}", draft_text)]
        if missing:
            f.append(f"ADVERSE ¶ WITHOUT RESPONSE: {missing[:12]}{'…' if len(missing) > 12 else ''}")
    if g.get("general_denial_forbidden"):
        for m in re.finditer(r"(?m)^\d+\..*?\)\s+(DENIED[^.]*\.)\s*(.*)$", draft_text):
            if not m.group(2).strip() or GENERAL_DENIAL.match(m.group(2)):
                f.append(f"GENERAL DENIAL: {m.group(0)[:80]}")
    for m in re.finditer(r"(?m)^(\d+)\. \(Defendant's ¶\d+:.*?\)\s+(?:ADMITTED|DENIED|CONCLUSION OF LAW)[^\n]*$", draft_text):
        if not COMPL_CITE.search(m.group(0)) and "ADMITTED." not in m.group(0):
            f.append(f"DEPARTURE: ¶{m.group(1)} response cites no Compl. ¶")
    if ARG_HEADING.search(draft_text):
        f.append("ARGUMENT HEADING in a pleading (departure)")
    return f


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("spec"); ap.add_argument("adverse"); ap.add_argument("complaint"); ap.add_argument("out")
    ap.add_argument("--cells", nargs="+", default=[]); ap.add_argument("--pairing"); ap.add_argument("--face")
    ap.add_argument("--cards", nargs="+", default=[], help="room cards files holding the INSTRUMENT decision (VERDICT SUSTAINED)")
    ap.add_argument("--case", default=""); ap.add_argument("--court", default="PA_CP_PHILA")
    ap.add_argument("--llm", default=os.environ.get("M1_LLM_URL", "http://localhost:8000/v1"))
    ap.add_argument("--no-guard", action="store_true", help="fixture only: skip the instrument-decision guard")
    a = ap.parse_args()
    spec = load_spec(a.spec)
    dec = instrument_decision(a.cards or a.cells)
    stop = None if a.no_guard else class_guard(spec, dec)
    if stop:
        print(stop); sys.exit(4)
    r = assemble(spec, a.adverse, a.complaint, a.cells, a.out, a.case, a.court, a.llm, pairing_path=a.pairing, face_path=a.face, decision=dec)
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r.get("ok") else 1)
