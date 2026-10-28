"""cards_to_cells — relay-room cards (+ skeptic verdicts) → m2 cells.jsonl, the input pipeline/m2/bank_bridge.py already
takes. The bridge is the PEN (m1_rebuttal.assemble + voice_pass, guardrail quote-in-source); this file only converts.
Agnostic. Only skeptic-SUSTAINED cards with a PROPOSITION (the court-facing sentence the bot wrote) become cells.

S-room-axis-forms: the room is a shared chat where each bot specialises on its axis, so a lane's cards are binned by the
lane's FORM (LANE_FORM table — lane name → form; it maps names, it does not reason):
  violation (default) : "Issue <n> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Violation: <record fact>"   → cell kind=violation, claim=<n>
  posture             : "Vehicle: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Trigger: <event+date>. Deadline rule: <verbatim>.
                         Standard: <verbatim>. Must contain: <items>."                                      → kind=posture, claim=0
  remedy              : "Relief <n>: <ask> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Because: <record fact>"  → kind=remedy, claim=<n>|0
A posture/remedy lane that (still) wrote the violation form is parsed from that form (form="legacy-violation"), never set aside.
A card naming a point beyond the units list is appended to <inputs>/NEW_UNITS.md and kept with new_unit=true.
GROUNDED only when the quote verifies in the corpus via card_gate (THE verifier): the card's named chunk, else its FTS search.
S-card-unit-pin (run 53): a violation card is pinned to its unit by CONTENT (scored overlap vs the unit headings/bodies, env ROOM_UNITS_MD =
ADVERSE_POINTS.md, else <inputs>/verified_record.md); the lane's 'Issue n' is a hint. Per-card log → sidecar.unit_pins {unit, method, score}.
usage: cards_to_cells.py <tag> <room_inputs_dir> <out_cells.jsonl> [cards_dir]"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, sys, json, glob, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import card_gate as _cg
except Exception:   # the verifier is the gate; without it rows can only inherit the gate marker (reported in the summary)
    _cg = None

META_AXES = {"skeptic"}                                                  # skeptic grades; it never writes cells
PROOF_AXES = {"proof"}                                                   # proof's CONTRADICTION cards → sidecar.contradictions (record facts, not cells)
# lane name → card form. posture-vehicle: every card is posture. dismissal-posture / jurisdiction: a card is posture when it is
# in the Vehicle: form or carries no violation (clause 'None …') or quotes a deadline rule; otherwise it is a violation card.
LANE_FORM = {"posture-vehicle": "posture", "dismissal-posture": "posture?", "jurisdiction": "posture?", "remedy": "remedy", "vacatur-sequence": "remedy", "vehicle-foreclosure": "posture"}
# PA authority family → chunk source prefix (used only to PREFER an in-family FTS hit; the card's own chunk is tried first)
PA_FAMILY = {"Pa.C.S": "pa_consolidated_statutes", "Pa.R.A.P": "pa_code_t210", "Pa.R.C.P": "pa_code_t231", "Pa. Code ch. 33": "pa_code_t207"}
META_RX = re.compile(r"bracketed substitution|verbatim accurac|quote (uses|is|matches)|substantively accurate|cannot verify verbatim|"
                     r"should be titled|correct procedural vehicle|sur-reply (is|should|must)|the draft|the room|cards?\b.*\bgate", re.I)
STATUS_RX = re.compile(r"(SUSTAINED|OPEN|REJECTED|NOT IN CORPUS)\b")
CITE_RX = r"\d+ [A-Z][A-Za-z.0-9]* ?\d+|Pa\.R\.C\.P|Phila|Pa\.C\.S|P\.S\.|U\.S\.C|§|_chunk_\d+|#[0-9a-f]{6}|NOT IN CORPUS|\bv\. "
CHUNK_RX = r"[a-z0-9_]+(?:_chunk_\d+|#[0-9a-f]{6,})"
CORPUS_TAG_RX = re.compile(r"\[corpus:\s*([A-Za-z0-9_#.-]+)\]")
QUOTE_RX = re.compile(r"[“\"]([^”\"]{4,500})[”\"]")
DATE_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})\b")
DEADLINE_RX = re.compile(r"within\s+(?:\d+|thirty|twenty|ten|sixty|ninety)\s*(?:\(\d+\)\s*)?days", re.I)
POSTURE_LABELS = ["Rule:", "Trigger:", "Deadline rule:", "Standard:", "Must contain:", "Prerequisites:", "Wins:", "Forecloses/sequences:", "Rank:"]
REMEDY_LABELS = ["Rule:", "Because:"]
VIOLATION_LABELS = ["Rule:", "Violation:"]


def parse_cards(tag, cards_dir):
    cards, verdicts, records = [], {}, []
    for f in sorted(glob.glob(f"{cards_dir}/{tag}_relay_r*_cards.md")):
        room = int(re.search(r"_r(\d+)_", f).group(1)); per_axis = {}
        for l in open(f, errors="ignore").read().splitlines():
            if l.startswith("CARD:"):
                p = [x.strip() for x in l[5:].split(" | ")]
                si = next((i for i in range(len(p) - 1, 1, -1) if STATUS_RX.match(p[i].upper())), None)
                if si is None and len(p) >= 2 and re.match(r"(Vehicle|Relief)\b[^|]{0,120}?(—|-) ?Rule:", p[1]):
                    # S-m2-blind-posture-r2: the posture / remedy FORM ("Vehicle: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. ...")
                    # is the whole card — it carries no `| where | authority | STATUS |` scaffold, so the status search found nothing
                    # and 97 of 104 posture cards were dropped SILENTLY (r2c). Recover deterministically: finding = the vehicle head,
                    # proposition = the form, status OPEN until grounded_status verifies the quote (the only thing that grounds anyway).
                    body = " | ".join(p[1:]); head = re.split(r"\s(—|-)\sRule:", body, 1)[0]
                    p = [p[0], head.strip(), "", "", "FORM", "PROPOSITION: " + body]; si = 4   # FORM: no self-grade; the verifier decides
                if si is None: continue
                n = per_axis[p[0]] = per_axis.get(p[0], 0) + 1
                tail = p[si + 1:]
                prop = next((x[len("PROPOSITION:"):].strip() for x in tail if x.upper().startswith("PROPOSITION:")), "")
                gate = " | ".join(x for x in tail if x.startswith("GATE:"))
                mid = p[2:si]
                is_auth = lambda x: (bool(re.search(CITE_RX, x[:60])) or bool(re.match(r"\[?" + CHUNK_RX, x))) and len(x) < 400
                auth = next((x for x in reversed(mid) if is_auth(x)), "")
                where = " ; ".join(x for x in mid if x != auth)
                cards.append({"room": room, "n": n, "axis": p[0], "finding": p[1], "where": where, "authority": auth,
                              "status": p[si].split()[0].upper(), "proposition": prop, "gate": gate, "line": l})
            elif l.startswith("VERDICT:"):
                p = [x.strip() for x in l[8:].split(" | ")]
                if len(p) >= 3:
                    m = re.search(r"\d+", p[1]); verdicts[(room, p[0], int(m.group(0)) if m else -1)] = p[2].split()[0].upper()
            elif l.startswith("RECORD:"):
                p = [x.strip() for x in l[7:].split(" | ")]
                if len(p) >= 2: records.append({"fact": p[0], "status": p[1].split()[0].upper(), "where": p[2] if len(p) > 2 else ""})
    return cards, verdicts, records


def ground_table(inputs):
    """citation ↔ chunk ids, from the deterministic ground table; rule chunks from the pack's forum section."""
    cite_by_slug, chunks_by_cite = {}, {}
    gt = open(f"{inputs}/ground_table.md", errors="ignore").read() if os.path.exists(f"{inputs}/ground_table.md") else ""
    for m in re.finditer(r"^## A\d+\. (.+?)\nchunk_id: (.+)$", gt, re.M):
        cite = re.sub(r"^See ", "", m.group(1).strip()); ids = re.findall(CHUNK_RX, m.group(2))
        chunks_by_cite[cite] = ids
        for cid in ids: cite_by_slug[cid.split("#")[0].split("_chunk_")[0]] = cite
    pack = open(f"{inputs}/ground_pack.md", errors="ignore").read() if os.path.exists(f"{inputs}/ground_pack.md") else ""
    for m in re.finditer(r"^### R\. (.+?) — .*?\nsource: (\S+)", pack, re.M):
        chunks_by_cite.setdefault(m.group(1).strip(), []).append(m.group(2))
    load_pack_official(inputs)
    return cite_by_slug, chunks_by_cite


def grounds(inputs):
    pack = open(f"{inputs}/ground_pack.md", errors="ignore").read() if os.path.exists(f"{inputs}/ground_pack.md") else ""
    return [(f"G{m.group(1)}", re.sub(r"^[A-Z]\. ", "", m.group(2)).strip()) for m in re.finditer(r"^G(\d+)\. (.+?)\s+\([^)]+\)\s*$", pack, re.M)]


def authority_of(field, cite_by_slug, chunks_by_cite):
    """authority field → {cite, quote, source}. cite from the table when the field carries a chunk id; source = that chunk."""
    if not field or "NOT IN CORPUS" in field or field in ("—", "-"): return None
    cid = (re.search(CHUNK_RX, field) or [None])[0] if re.search(CHUNK_RX, field) else None
    q = re.search(r"[“\"]([^”\"]{12,500})[”\"]", field); quote = q.group(1).strip() if q else ""
    cite = ""
    if cid: cite = cite_by_slug.get(cid.split("#")[0].split("_chunk_")[0], "")
    if not cite:
        head = re.sub(r"^\[?" + CHUNK_RX + r"\]?\s*", "", field).split(" — ")[0].strip().strip("[]")
        if re.search(CITE_RX, head[:60]) and not re.match(r"\[?" + CHUNK_RX, head): cite = head
    if not cite: return None
    norm = lambda c: re.sub(r"[^a-z0-9]", "", c.lower())
    src = cid or next((ids[0] for c, ids in chunks_by_cite.items() if norm(c)[:18] and norm(c)[:18] in norm(cite) or norm(cite)[:18] in norm(c)), "")
    # a rule cited as "Phila. Civ. R. *1028(c)(1)(b)" → the forum-section source for *1028(c)
    if not src:
        rk = re.match(r"(Phila\.? ?Civ\.? ?R\.? ?\*?\d+(?:\.\d+)?(?:\([a-z0-9]+\))?|Pa\.R\.C\.P\. ?\d+(?:\([a-z0-9]+\))?)", cite)
        if rk:
            for c, ids in chunks_by_cite.items():
                if norm(c).startswith(norm(rk.group(1))[:12]): src = ids[0]; break
    return {"cite": cite, "quote": quote, "source": src}


def _point_heads(inputs) -> dict:
    """Point number -> heading, from verified_record.md ('## Point n. <heading>')."""
    try:
        txt = open(f"{inputs}/verified_record.md", encoding="utf-8").read()
    except Exception:
        return {}
    return {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"(?m)^## (?:Point|Issue) (\d{1,2})\.\s*(.+)$", txt)}


# ---------------------------------------------------------------- S-room-axis-forms: the three card forms
def _split_labels(text, labels):
    """'Head — Rule: … Trigger: … ' → {'head': …, 'Rule': …, 'Trigger': …}; a label absent from the text → key absent (null field)."""
    pos = [(m.start(), m.group(0)) for lab in labels for m in [re.search(r"(?<![A-Za-z])" + re.escape(lab), text)] if m]
    pos.sort()
    out = {"head": (text[:pos[0][0]] if pos else text).strip(" —-:")}
    for i, (st, lab) in enumerate(pos):
        en = pos[i + 1][0] if i + 1 < len(pos) else len(text)
        out[lab.rstrip(":")] = text[st + len(lab):en].strip().rstrip(".").strip()
    return out


CITE_START_RX = re.compile(r"(\d+\s+Pa\.C\.S\.|\d+\s+P\.S\.|\d+\s+U\.S\.C\.|\d+\s+Pa\. ?Code|Pa\.R\.[A-Z]\.P\.|Pa\.R\.E\.|Phila\. ?Civ\. ?R\.|Fed\. ?R\. ?\w+\. ?P\.|"
                           r"\b(?:Code of Judicial Conduct|Rule \d+(?:\.\d+)?(?:\([A-Za-z0-9]+\))*)\b|[A-Z][\w.'-]+(?: [A-Z][\w.'-]+)* v\. )")


def _rule_parts(rule):
    """'“<verbatim>” <cite> [corpus:<chunk>]' → (rule_verbatim, cite, chunk). Quote = the longest quoted span; cite = the citation
    that follows it (first token that opens a citation: '42 Pa.C.S.', 'Pa.R.A.P.', 'X v. '…); words between quote and cite are paraphrase."""
    rule = rule or ""
    qs = QUOTE_RX.findall(rule); quote = max(qs, key=len).strip() if qs else None
    cm = CORPUS_TAG_RX.search(rule); chunk = cm.group(1) if cm else None
    rest = CORPUS_TAG_RX.sub("", rule)
    after = (rest.split(quote, 1)[1] if quote and quote in rest else rest)
    after = re.sub(r"^[”\"\s,.;—-]+", "", after)
    cs = CITE_START_RX.search(after)
    cite = (after[cs.start():] if cs else after).strip(" .;,")
    cite = re.sub(r"\s+", " ", cite) or None
    return quote, cite, chunk


def _clause_with(text, needle, limit=260):
    """the sentence of `text` that carries `needle`, copied — sentence ends are '. ' after a lowercase letter or ')' (not 'No.' / 'Pa.')."""
    if not text or not needle or needle not in text: return None
    i = text.find(needle)
    starts = [m.end() for m in re.finditer(r"(?<=[a-z\)][.;])\s+(?=[A-Z])", text[:i])]
    st = starts[-1] if starts else 0
    em = re.search(r"(?<=[a-z\)][.;])\s+(?=[A-Z])", text[i:])
    en = i + em.start() + 1 if em else len(text)
    return text[st:en].strip()[:limit] or None


def _deadline_from_chunk(source, quote):
    """the VERBATIM sentence of the verified corpus chunk that carries the quote and a 'within N days' deadline (None when absent)."""
    if not (source and quote and _cg): return None
    try:
        _, txt = _cg._chunk(source)
    except Exception:
        return None
    if not txt: return None
    seg = _cg.norm(_cg.segments(quote)[0] if _cg.segments(quote) else quote)
    for s in re.split(r"(?<=[a-z\)][.;])\s+(?=[A-Z(])", re.sub(r"\s+", " ", txt)):
        if seg in _cg.norm(s) and DEADLINE_RX.search(s):
            s = re.sub(r"\.\s+\S*\d+[a-z]\d{3,}\S*.*$", ".", s.strip())      # drop the corpus section token / cross-reference tail after the sentence
            return s[:400]
    return None


def classify_form(axis, proposition):
    """lane + proposition → 'violation' | 'posture' | 'remedy' (the lane table decides; the text only decides the '?' lanes)."""
    f = LANE_FORM.get(axis, "violation")
    if f == "posture?":
        pv = re.match(r"\s*Vehicle:", proposition or "")
        parts = _split_labels(proposition or "", VIOLATION_LABELS)
        no_violation = bool(re.match(r"\s*None\b", parts.get("Violation", "x")))
        q, _, _ = _rule_parts(parts.get("Rule", ""))
        return "posture" if (pv or no_violation or (q and DEADLINE_RX.search(q))) else "violation"
    return f


def parse_posture(proposition, finding="", where=""):
    """Vehicle form → the contract fields; a violation-form card from a posture lane is mapped field-by-field (form=legacy-violation)."""
    text = proposition or ""
    # "Vehicle: <name>" (the form) and "Vehicle <n> of ALL: <name>" (the ranked form a blind posture lane writes) are the same form;
    # r2c: the ranked head missed this match, fell to legacy mapping, and every field (cite, quote, trigger, deadline) landed in title.
    vm = re.match(r"\s*Vehicle(?:\s+(\d+)\s+of\s+\w+)?\s*:\s*", text)
    if vm:
        d = _split_labels(text[vm.end():], POSTURE_LABELS)
        if vm.group(1): d["rank"] = int(vm.group(1))
        quote, cite, chunk = _rule_parts(d.get("Rule", ""))
        trig = d.get("Trigger"); dm = DATE_RX.search(trig or "")
        must = d.get("Must contain")
        return {"form": "posture", "rank": d.get("rank"), "vehicle": d["head"] or None, "rule_verbatim": quote, "cite": cite, "chunk": chunk,
                "trigger_event": trig or None, "trigger_date": dm.group(1) if dm else None,
                "deadline_rule": d.get("Deadline rule") or None, "standard": d.get("Standard") or None,
                "prerequisites": d.get("Prerequisites") or None, "wins": d.get("Wins") or None,
                "forecloses": d.get("Forecloses/sequences") or None, "rank_why": d.get("Rank") or None,
                "must_contain": [x.strip(" .") for x in re.split(r";|\s\(\w\)\s|\s\d\)\s", must) if x.strip(" .")] if must else None}
    d = _split_labels(text, VIOLATION_LABELS)
    quote, cite, chunk = _rule_parts(d.get("Rule", ""))
    viol = d.get("Violation", ""); rule_clause = CORPUS_TAG_RX.sub("", d.get("Rule", "")).strip()
    vm = re.search(r"\b(motion (?:to|for) \w+(?: (?!under\b|of\b|the\b|to\b|in\b|for\b|within\b|is\b)\w+)?|petition for \w+(?: \w+)?|appeal)\b", finding + " " + viol, re.I)
    vehicle = (f"{vm.group(1)} under {cite}" if vm and cite else cite) or None
    dm = DATE_RX.search(viol) or DATE_RX.search(finding)
    trig = _clause_with(viol if (dm and DATE_RX.search(viol)) else finding, dm.group(1)) if dm else None
    if trig: trig = re.sub(r"^None\b[^;]*;\s*", "", trig)   # 'None alleged; this is the vehicle …' → the vehicle clause
    # legacy form: the card's rule clause names the deadline outside the quote marks → kept as the card's words, provenance 'card'
    dl = rule_clause if DEADLINE_RX.search(rule_clause) else None
    return {"form": "legacy-violation", "vehicle": vehicle, "rule_verbatim": quote, "cite": cite, "chunk": chunk,
            "trigger_event": trig, "trigger_date": dm.group(1) if dm else None,
            "deadline_rule": dl, "deadline_rule_source": "card" if dl else None, "standard": None, "must_contain": None}


def parse_remedy(proposition, finding=""):
    """Relief form → the contract fields; a violation-form card from the remedy lane maps finding→relief_item, Violation→record_fact."""
    text = proposition or ""
    m = re.match(r"\s*Relief\s+(\d{1,2})\s*:\s*", text)
    if m:
        d = _split_labels(text[m.end():], REMEDY_LABELS)
        quote, cite, chunk = _rule_parts(d.get("Rule", ""))
        return {"form": "remedy", "n": int(m.group(1)), "relief_item": d["head"] or None, "rule_verbatim": quote, "cite": cite,
                "chunk": chunk, "record_fact": d.get("Because") or None}
    d = _split_labels(text, VIOLATION_LABELS)
    quote, cite, chunk = _rule_parts(d.get("Rule", ""))
    im = re.match(r"\s*(?:Issue|Point|Ground)\s+(\d{1,2})\b", text)
    return {"form": "legacy-violation", "n": int(im.group(1)) if im else None, "relief_item": finding or None, "rule_verbatim": quote,
            "cite": cite, "chunk": chunk, "record_fact": d.get("Violation") or None}


def verify_quote(quote, cite, chunk):
    """THE verifier is card_gate (same as the room gate / cells_to_ground.reverify). Order: the card's own chunk must carry every
    segment of the quote; else an FTS hit in the cite's PA family (source prefix); else card_gate.verify_phrase. → dict|None."""
    if not quote or _cg is None: return None
    try:
        segs = [_cg.norm(s) for s in _cg.segments(quote)] or [_cg.norm(quote)]
        if chunk:
            src, txt = _cg._chunk(chunk)
            if txt and all(_cg._in_text(s, _cg.norm(txt)) for s in segs): return {"source": chunk, "how": "card chunk carries the quote"}
        fam = next((v for k, v in PA_FAMILY.items() if k in (cite or "")), None)
        if fam:
            for c, src, txt in _cg._candidates(segs[0]):
                if (src or "").lower().startswith(fam) and all(_cg._in_text(s, _cg.norm(txt)) for s in segs):
                    return {"source": c, "how": f"fts hit in {fam}; card chunk {chunk} did not carry the quote"}
        v = _cg.verify_phrase(quote, cite or "")
        if v["status"] == "verified": return {"source": v["chunk_id"], "how": v["how"] + (f"; card chunk {chunk} did not carry the quote" if chunk else "")}
    except Exception as e:   # DB unavailable → caller falls back to the gate marker
        return {"error": str(e)[:80]}
    return None


def normalize_cells(cells):
    """S-cite-drift-normalize: pin every CASE authority of every cell to its corpus source (cite_normalize.normalize_row via
    card_gate.cite_normalizer — the ONE pinner). -> [{action, old, new, source, why, relay}] for normalized / FETCH verdicts."""
    if _cg is None or not hasattr(_cg, "cite_normalizer"): return []
    cn, R = _cg.cite_normalizer()
    if not cn: return []
    out = []
    for cell in cells:
        try:
            for res in cn.normalize_row(cell, R):
                if res["action"] in ("normalized", "fetch"): out.append(dict(res, relay=cell.get("relay")))
        except Exception as e:
            out.append({"action": "error", "why": str(e)[:120], "relay": cell.get("relay")})
    return out


def _gate_corpus_hit(gate):
    """'GATE: quote verified (<chunk>)' with a corpus chunk (S-09: a .md/.txt doc hit is a record fact, not corpus authority)."""
    gm = re.search(r"GATE: quote verified \(([^)]+)\)", gate or "")
    return gm.group(1) if gm and not gm.group(1).endswith((".md", ".txt")) else None


_PACK_OFFICIAL = []   # [(source, body)] — ground-pack forum rows + DEEPEN rows whose text came from the official rules index


def load_pack_official(inputs):
    """S-m2-blind-posture-r2: PA rule / Pa.C.S. text in the ground pack is resolved from pipeline.rules_index (the official Pa. Code /
    Pa.C.S. ingest), which is NOT in the chunk-DB FTS — so a lane quoting a forum row verbatim could never be GROUNDED by
    verify_quote and the posture lane sat at 0 grounded. A quote that is carried by a forum row / DEEPEN row with a `source:`
    label is grounded as [official:<source>] (the discipline's own tag), not left OPEN."""
    global _PACK_OFFICIAL
    pack = open(f"{inputs}/ground_pack.md", errors="ignore").read() if os.path.exists(f"{inputs}/ground_pack.md") else ""
    rows = []
    for m in re.finditer(r"^### R\. (.+?) — .*?\nsource: (\S+).*?\n(?:chunk: .*?\n)?> (.+?)\n", pack, re.M | re.S):
        rows.append((f"official:{m.group(2)}", m.group(3)))
    for m in re.finditer(r"^- (.+?)  chunk: (\S+).*?\n  > (.+?)\n", pack, re.M):
        rows.append((m.group(2) if m.group(2) != "NONE" else f"official:{m.group(1)}", m.group(3)))
    _PACK_OFFICIAL = rows
    return len(rows)


def _pack_official_hit(quote):
    if not quote or _cg is None: return None
    try:
        nq = _cg.norm(quote)
        segs = [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", nq) if x]
        for src, body in _PACK_OFFICIAL:
            nb = _cg.norm(body)
            if segs and all(_cg._in_text(x, nb) for x in segs): return src
    except Exception:
        return None
    return None


def grounded_status(quote, cite, chunk, gate):
    """→ (status, source, how). GROUNDED when the quote verifies in the corpus, OR is carried verbatim by an official-source
    ground-pack row (rules index text that the chunk-DB does not hold)."""
    v = verify_quote(quote, cite, chunk)
    if v and "source" in v: return "GROUNDED", v["source"], v["how"]
    gh = _gate_corpus_hit(gate)
    if gh: return "GROUNDED", gh, "gate marker" + (f" (verifier unavailable: {v['error']})" if v and "error" in v else "")
    ph = _pack_official_hit(quote)
    if ph: return "GROUNDED", ph, "ground-pack official row carries the quote"
    if not quote: return "OPEN", chunk, "no verbatim quote"
    return "OPEN", chunk, "quote not verified in corpus" + (f" (verifier unavailable: {v['error']})" if v and "error" in v else "")


def _new_units_md(inputs, new_units):
    """append '**N. <issue text>**' + the card line to <inputs>/NEW_UNITS.md (idempotent: numbers already present are skipped)."""
    path = f"{inputs}/NEW_UNITS.md"
    have = set(int(m.group(1)) for m in re.finditer(r"(?m)^\*\*(\d{1,2})\. ", open(path, errors="ignore").read())) if os.path.exists(path) else set()
    add = [(n, t, line) for n, t, line in new_units if n not in have]
    if not add and os.path.exists(path): return path
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8") as f:
        if fresh:
            f.write("# NEW UNITS — points named by room cards beyond the units list (appended by cards_to_cells; review before adoption)\n\n")
        for n, t, line in sorted(add):
            f.write(f"**{n}. {t}**\n{line}\n\n")
    return path


# ---------------------------------------------------------------- S-card-unit-pin (run 53): deterministic card → unit pin by CONTENT
# Run 52: lanes numbered their cards 'Issue 117/118/121' (their own count), and a card was pinned to a unit ONLY by that number →
# 10 of 17 units had zero rows while the cards about them sat in NEW_UNITS. The number a lane writes is a HINT; the GUARDRAIL is a
# scored overlap of the card's own words (finding + proposition, MIRROR clause and the order boilerplate stripped) against each
# unit's heading (weight 3) + body (weight 1), idf-weighted over the units so shared boilerplate ('with prejudice', 'Counts II, III,
# V and VI', the ORDER UNDER REVIEW block) scores nothing. Decision (per card, logged to the sidecar as unit/method/score):
#   number   the lane's number names a real unit AND the content agrees (that unit is the content's best, or within AGREE of it)
#   content  content is confident (best ≥ MIN_SCORE and second ≤ AMBIG × best) — the number (absent, foreign or disagreeing) is overruled
#   hint     content ambiguous but the number names a real unit → the number
#   weak     no usable number; content best ≥ WEAK_SCORE → best (flagged)
#   none     nothing usable → set aside (or NEW_UNITS when the lane numbered it, as before)
# Thresholds tuned offline on r49 + r52 cards (scratch/eval_unit_pin.py): r49 number-pins reproduced ≥ 95 %.
PIN_MIN_SCORE, PIN_AMBIG, PIN_AGREE, PIN_WEAK_SCORE = 0.12, 0.85, 0.80, 0.06
_STOP = set("""the of and a an to in that is are was were be been for on by with as at or it its this these those which who whom whose
from not no nor but if then than so such any all each every both either neither under over into onto out up down about above below
between among through during before after without within against because while where when whether while may must shall should would
could can will does did do done has have had having here there their they them he she his her him we us our you your i me my one two
three four five six also only even still yet very more most less least much many few own same other another per via et al e.g i.e
order court plaintiff defendant progressive counts count control entered docketed dismissed dismisses dismissal sustained objections
preliminary rule rules record facts fact issue point ground violation mirror reasons stated states stating respectively hereby further
ordered whereby complaint motion filed file pa pennsylvania 2026 2025 october ctl no""".split())
_BOILER_RX = [re.compile(p, re.I) for p in (
    r"ORDER_\d+\s*\(Control No\.?\s*\d+,?\s*entered\s*[\d-]+\)", r"Control No\.?\s*\d{6,}", r"entered\s+\d{4}-\d{2}-\d{2}",
    r"Counts?\s+II,?\s+III,?\s+V,?\s+(?:and\s+)?VI\b", r"\[corpus:[^\]]*\]", r"\bMIRROR:.*$", r"\bIssue\s+\d{1,3}\s*[—:-]*", r"(?<![A-Za-z])Rule:\s*")]


def _pin_tokens(text):
    """lowercase word / citation tokens, stopwords out; '2.11(a)', '1028(a)(4)', '240(j)', '1171.4', 'utpcpl' survive as one token."""
    t = text or ""
    for rx in _BOILER_RX: t = rx.sub(" ", t)
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9.()§'-]*", t.lower()):
        w = w.strip(".()'-")
        if len(w) < 2 or w in _STOP or w.isdigit() and len(w) < 2: continue
        out.append(w)
    return out


def unit_profiles(inputs, units_md=None):
    """unit n -> {'title', 'tokens': {token: weight}} from ADVERSE_POINTS.md ('**n. title**' blocks; env ROOM_UNITS_MD or arg) else
    <inputs>/verified_record.md ('## Point n. title' blocks). Title tokens weigh 3 (title bigrams 4), body tokens 1; then idf over units."""
    import math
    path = units_md or os.environ.get("ROOM_UNITS_MD") or ""
    blocks = []
    if path and os.path.exists(path):
        txt = open(path, encoding="utf-8", errors="ignore").read()
        heads = list(re.finditer(r"(?m)^\*\*(\d{1,2})\.\s+(.+?)\*?\*?\s*$", txt))
        for i, m in enumerate(heads):
            body = txt[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            blocks.append((int(m.group(1)), m.group(2).strip("* "), body))
    if not blocks:
        try: txt = open(f"{inputs}/verified_record.md", encoding="utf-8", errors="ignore").read()
        except Exception: txt = ""
        heads = list(re.finditer(r"(?m)^## (?:Point|Issue) (\d{1,2})\.\s*(.+)$", txt))
        for i, m in enumerate(heads):
            body = txt[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            blocks.append((int(m.group(1)), m.group(2).strip(), body))
    prof = {}
    for n, title, body in blocks:
        w = {}
        tt = _pin_tokens(title)
        for tok in tt: w[tok] = max(w.get(tok, 0), 3.0)
        for a, b in zip(tt, tt[1:]): w[a + "_" + b] = 4.0
        for tok in set(_pin_tokens(body[:2500])): w.setdefault(tok, 1.0)
        prof[n] = {"title": title, "w": w}
    N = len(prof) or 1
    df = {}
    for p in prof.values():
        for tok in p["w"]: df[tok] = df.get(tok, 0) + 1
    for p in prof.values():
        vec = {tok: wt * (0.0 if df[tok] >= max(2, 0.6 * N) else math.log((N + 1) / (df[tok] + 0.5))) for tok, wt in p["w"].items()}
        vec = {k: v for k, v in vec.items() if v > 0}
        p["vec"] = vec; p["norm"] = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return prof


def pin_unit(card_text, number, profiles):
    """-> {'unit': n|None, 'method': number|content|hint|weak|none, 'score': best, 'best': b, 'second': s2, 'number': number}."""
    import math
    toks = _pin_tokens(card_text)
    ctoks = set(toks) | {a + "_" + b for a, b in zip(toks, toks[1:])}
    if not profiles or not ctoks: return {"unit": number if number in profiles else None, "method": "hint" if number in profiles else "none", "score": 0.0, "best": None, "second": 0.0, "number": number}
    cnorm = math.sqrt(len(ctoks))
    sc = {n: sum(p["vec"].get(t, 0.0) for t in ctoks) / (p["norm"] * cnorm) for n, p in profiles.items()}
    ranked = sorted(sc.items(), key=lambda kv: -kv[1]); b, s1 = ranked[0]; s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    res = {"score": round(s1, 4), "best": b, "second": round(s2, 4), "number": number}
    if number in profiles and s1 > 0 and (number == b or sc[number] >= PIN_AGREE * s1): res.update(unit=number, method="number")
    elif s1 >= PIN_MIN_SCORE and s2 <= PIN_AMBIG * s1: res.update(unit=b, method="content")
    elif number in profiles: res.update(unit=number, method="hint")
    elif s1 >= PIN_WEAK_SCORE: res.update(unit=b, method="weak")
    else: res.update(unit=None, method="none")
    return res


def gate_deadline(cell, forum="pa"):
    """deterministic deadline for a posture cell: analysis_runner.compute_deadline(trigger_date, deadline_rule, forum) —
    Rule 106 / 1 Pa.C.S. § 1908 (pa) or FRCP 6(a) (fed) counting. The lane's own date, if any, is never used.
    Returns {deadline_date, deadline_days, deadline_counting, deadline_why}; {} when the runner is unavailable."""
    try:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        root = str(_eroot())
        if root not in sys.path: sys.path.append(root)
        from pipeline.m2.analysis_runner import compute_deadline
    except Exception as e:
        return {"deadline_date": None, "deadline_why": f"deadline computer unavailable: {e!r}"[:160]}
    c = compute_deadline(cell.get("trigger_date"), cell.get("deadline_rule") or "", forum)
    return {"deadline_date": c["date"], "deadline_days": c["days"], "deadline_counting": c["counting"], "deadline_why": c["why"]}


def main(tag, inputs, out_path, cards_dir=str(_eroot() / "output/research/rooms_ab")):
    cards, verdicts, records = parse_cards(tag, cards_dir)
    cite_by_slug, chunks_by_cite = ground_table(inputs); G = grounds(inputs); points = _point_heads(inputs)
    UPROF = unit_profiles(inputs)                                        # S-card-unit-pin: the units' own words (ROOM_UNITS_MD else verified_record.md)
    for _k, _p in UPROF.items(): points.setdefault(_k, _p["title"])     # a re-queue leg's record lists only its units; the pin may name any unit
    cells, side, contradictions, new_units, open_lanes, pin_log = [], [], [], {}, [], []
    for c in cards:
        v = verdicts.get((c["room"], c["axis"], c["n"]))
        ok = v == "SUSTAINED" or (v is None and c["status"] == "SUSTAINED" and "GATE: quote NOT" not in c["gate"] and "carries no quote" not in c["gate"])
        # a form-only posture/remedy card (status FORM) carries no self-grade and — skeptic silent — no verdict; it is admitted to the
        # cell stage where grounded_status() verifies its verbatim rule against the corpus / official rows. The verifier, not a status
        # word, decides GROUNDED vs OPEN. (r2c: 100 such cards were set aside as "verdict OPEN" with skeptic SILENT = posture 0.)
        ok = ok or (v is None and c["status"] == "FORM")
        why = None
        if c["axis"] in META_AXES: why = "skeptic lane (grades, never a cell)"
        elif c["axis"] in PROOF_AXES:
            why = "proof lane → sidecar.contradictions"; contradictions.append({"finding": c["finding"], "where": c["where"], "status": c["status"], "verdict": v, "proposition": c["proposition"], "card": c["line"][:400]})
        elif META_RX.search(c["finding"]) or META_RX.search(c["proposition"]): why = "meta/self-check"
        elif not ok: why = f"verdict {v or c['status']}"
        elif not c["proposition"]: why = "no PROPOSITION"
        if why:
            side.append({"why": why, "card": c["line"][:300]})
            if why.startswith("verdict"):
                pm = re.match(r"\s*(?:Issue|Point|Ground|Relief)\s+(\d{1,2})\b", c["proposition"])
                open_lanes.append({"lane": c["axis"], "point": int(pm.group(1)) if pm else None, "verdict": v or c["status"], "relay": f"{tag} r{c['room']} {c['axis']}#{c['n']}"})
            continue
        form = classify_form(c["axis"], c["proposition"])
        relay = f"{tag} r{c['room']} {c['axis']}#{c['n']}"
        a = authority_of(c["authority"], cite_by_slug, chunks_by_cite)
        # ---- posture row (claim 0): the vehicle, trigger, deadline, standard, must-contain — what the motion's opening + prayer are built from
        if form == "posture":
            p = parse_posture(c["proposition"], c["finding"], c["where"])
            status, src, how = grounded_status(p["rule_verbatim"], p["cite"], p["chunk"], c["gate"])
            dl_src = p.get("deadline_rule_source") if p.get("deadline_rule") else None
            if status == "GROUNDED" and p["form"] == "legacy-violation":
                # the verified chunk's own sentence beats the card's paraphrase: verbatim deadline words, provenance corpus:<chunk>
                vb = _deadline_from_chunk(src, p["rule_verbatim"])
                if vb: p["deadline_rule"], dl_src = vb, f"corpus:{src}"
            auth = [{"cite": p["cite"], "quote": p["rule_verbatim"], "source": src, "verified": status == "GROUNDED"}] if (p["cite"] or p["rule_verbatim"]) else ([a] if a else [])
            cells.append({"claim": 0, "title": p["vehicle"] or c["finding"][:120], "kind": "posture", "form": p["form"], "status": status, "level": 6, "bot": c["axis"],
                          "finding": c["finding"], "proposition": c["proposition"],
                          "vehicle": p["vehicle"], "rank": p.get("rank"), "cite": p.get("cite") or None, "prerequisites": p.get("prerequisites"), "wins": p.get("wins"), "forecloses": p.get("forecloses"), "rank_why": p.get("rank_why"),
                          "rule_verbatim": p["rule_verbatim"], "trigger_event": p["trigger_event"], "trigger_date": p["trigger_date"],
                          "deadline_rule": p["deadline_rule"], "deadline_rule_source": dl_src or ("card" if p["deadline_rule"] else None),
                          "standard": p["standard"], "must_contain": p["must_contain"], "authorities": auth,
                          "verify": how, "gap": None, "fix": None, "record_facts": [], "relay": relay})
            # S-m2-room-roster: the lane never states a deadline DATE (posture-vehicle spec, ported from the posture JOB.yaml);
            # the GATE computes it deterministically from the record trigger date + the rule's period words (opt-in ROOM_FORUM=pa|fed).
            if os.environ.get("ROOM_FORUM"):
                cells[-1].update(gate_deadline(cells[-1], os.environ["ROOM_FORUM"]))
            # a dismissal-posture/jurisdiction card that ALSO states a real violation keeps its violation row below (byte-identical to before)
            if c["axis"] == "posture-vehicle" or re.match(r"\s*Vehicle:", c["proposition"]): continue
            parts = _split_labels(c["proposition"], VIOLATION_LABELS)
            if re.match(r"\s*None\b", parts.get("Violation", "")) and not re.match(r"\s*(?:Issue|Point|Ground)\s+\d", c["proposition"]): continue
        # ---- remedy row: relief items for the prayer
        if form == "remedy":
            p = parse_remedy(c["proposition"], c["finding"])
            status, src, how = grounded_status(p["rule_verbatim"], p["cite"], p["chunk"], c["gate"])
            auth = [{"cite": p["cite"], "quote": p["rule_verbatim"], "source": src, "verified": status == "GROUNDED"}] if (p["cite"] or p["rule_verbatim"]) else ([a] if a else [])
            n = p["n"] or 0; new = bool(n) and n not in points
            if new: new_units.setdefault(n, (n, c["finding"], c["line"]))
            cells.append({"claim": n, "title": points.get(n, p["relief_item"] or c["finding"][:120]), "kind": "remedy", "form": p["form"], "status": status, "level": 6,
                          "bot": c["axis"], "finding": c["finding"], "proposition": c["proposition"],
                          "relief_item": p["relief_item"], "rule_verbatim": p["rule_verbatim"], "record_fact": p["record_fact"], "authorities": auth,
                          "verify": how, "new_unit": new, "gap": None, "fix": None, "record_facts": [], "relay": relay})
            continue
        # ---- violation row (unchanged logic; new points appended as units instead of set aside)
        is_face = c["finding"].startswith(("DEFECT", "CONTRADICTION", "VERIFY"))
        # claim title = the ground heading this card answers (by the authority the adverse filing uses under it), else the authority itself
        title = next((h for gid, h in G if a and re.sub(r"[^a-z]", "", (a["cite"].split(" v. ")[0].split()[-1] if " v. " in a["cite"] else a["cite"]).lower())[:8] in re.sub(r"[^a-z]", "", h.lower())), None)
        # a card that answers a NUMBERED point ("Issue 3 — …" / "Point 3 — …") is keyed to that point number
        # S-card-number-column (run 54): the lane's 'Issue n' may lead the PROPOSITION (r49/r52) or the FINDING column (r53/r54 lanes:
        # 'ISSUE 1 — RULE: …'), in either case. r54: 39 of 43 room-1 cards carried the number, 113 of 120 pins read None because only
        # the proposition was read, case-sensitively → content-only pin (scores .02-.06, below floor) → 7 of 17 units NOT CARDED.
        _ISSUE_RX = r"\s*(?:Issue|Point|Ground)\s+(\d{1,2})\b\s*[—:-]*\s*"
        _pn = re.match(_ISSUE_RX, c["proposition"], re.I)
        if _pn: c["proposition"] = c["proposition"][_pn.end():].strip()
        else: _pn = re.match(_ISSUE_RX, c["finding"] or "", re.I) or re.search(r"record:\s*(?:Point|Issue)\s*(\d{1,2})\b", c["where"] + " " + c["finding"], re.I)
        _rule_src = c["proposition"] if re.search(r"(?<![A-Za-z])Rule:", c["proposition"]) else (c["finding"] if re.search(r"(?<![A-Za-z])Rule:", c["finding"] or "") else "")
        if not a and _rule_src:
            # S-card-rule-authority (run 53): the Issue-form card carries its authority INSIDE the Rule clause ('Rule: “…” <cite> [corpus:<chunk>]'),
            # not in a separate column (r52 cards: 17 of 93 cells had an authority) — the quote/cite/chunk of that clause is the row's authority.
            # the Issue-form block may sit in the proposition (r49/r52 lanes) or in the finding column (r53 lanes) — same clause, same parts
            _q, _cite, _chunk = _rule_parts(_split_labels(_rule_src, VIOLATION_LABELS).get("Rule", ""))
            if _cite or _chunk: a = {"cite": _cite or "", "quote": _q or "", "source": _chunk or ""}
        if a:
            # verified ⇔ the gate's own hit (S-07/S-09): 'GATE: quote verified (<chunk>)' — partial/NOT lines never count
            gm = re.search(r"GATE: quote verified \(([^)]+)\)", c["gate"])
            a["verified"] = bool(a.get("quote")) and bool(gm) and not gm.group(1).endswith((".md", ".txt"))   # [doc] hit ≠ corpus authority (S-09)
            if gm and re.match(CHUNK_RX + r"$|[0-9a-f]{16,}$|usc_t\d+_s\w+$|amjur2d_\w+$", gm.group(1)): a["source"] = gm.group(1)
            if not a["verified"] and a.get("quote"):
                # S-card-rule-authority: the room gate graded only 50 of 194 r52 cards and checks the card's named chunk only — an
                # unverified quote is verified HERE by the same verifier with its full ladder (card chunk → FTS in the cite's family →
                # verify_phrase); a quote the ladder cannot find stays unverified (never pleaded).
                v = verify_quote(a["quote"], a.get("cite") or "", a.get("source") or None)
                if v and "source" in v: a["verified"], a["source"], a["verify"] = True, v["source"], v["how"]
        # S-08 + S-card-unit-pin: claim MUST be a unit number (what m1_screen keys on). The lane's number is a HINT; the pin is the
        # content score against the units (pin_unit) — number kept only when it names a real unit and the content agrees or is ambiguous.
        _num = int(_pn.group(1)) if _pn else None
        pin = pin_unit(c["finding"] + " " + c["proposition"], _num, UPROF)
        pin_log.append({"relay": relay, "lane": c["axis"], "number": _num, "unit": pin["unit"], "method": pin["method"], "score": pin["score"],
                        "best": pin["best"], "second": pin["second"], "card": c["proposition"][:120]})
        _n = pin["unit"]
        if _n is None and _num is not None and _num not in points: _n = _num        # lane-numbered point content cannot place → NEW_UNITS (as before)
        if _n is None:
            side.append({"why": "claim not pinned to a unit (no usable 'Issue n'; content score below floor)", "pin": pin, "card": c["line"][:300]}); continue
        new = _n not in points
        if new: new_units.setdefault(_n, (_n, c["finding"], c["line"]))
        _claim = _n; _title = points.get(_n, c["finding"][:160])
        cells.append({"claim": _claim, "title": _title,
                      "status": "GROUNDED", "level": "record" if is_face or not a else 6, "bot": c["axis"],
                      "finding": c["finding"], "proposition": c["proposition"],
                      "authorities": [a] if a else [], "gap": None, "fix": None,
                      "record_facts": [c["finding"] + (f" ({c['where']})" if c["where"] else "")] if is_face else [],
                      "relay": relay, **({"new_unit": True, "kind": "violation"} if new else {})})
    nu_path = _new_units_md(inputs, list(new_units.values())) if new_units else None
    cite_log = normalize_cells(cells)                                   # S-cite-drift-normalize: room cells carry the corpus pin
    with open(out_path, "w") as f:
        for cell in cells: f.write(json.dumps(cell, ensure_ascii=False) + "\n")
    json.dump({"set_aside": side, "records": records, "contradictions": contradictions, "open_by_lane": open_lanes,
               "new_units": sorted(new_units), "new_units_md": nu_path, "cite_normalize": cite_log, "unit_pins": pin_log},
              open(out_path.replace(".jsonl", "_sidecar.json"), "w"), indent=1, ensure_ascii=False)
    import collections
    kind = lambda c: c.get("kind", "violation")
    print(json.dumps({"cards": len(cards), "cells": len(cells),
                      "by_kind": dict(collections.Counter(kind(c) for c in cells)),
                      "with_authority": sum(1 for c in cells if c["authorities"]),
                      "grounded": {k: sum(1 for c in cells if kind(c) == k and c["status"] == "GROUNDED") for k in ("posture", "remedy")},
                      "set_aside": dict(collections.Counter(s["why"] for s in side)),
                      "open_by_lane": [f"{o['lane']}#{o['point']}:{o['verdict']}" for o in open_lanes],
                      "new_units": sorted(new_units), "contradictions": len(contradictions),
                      "unit_pin": {"methods": dict(collections.Counter(p["method"] for p in pin_log)), "units_with_rows": len({c["claim"] for c in cells if isinstance(c["claim"], int) and c["claim"] in UPROF}), "units": len(UPROF),
                                   "number_overruled": sum(1 for p in pin_log if p["number"] in UPROF and p["unit"] != p["number"])},
                      "verifier": "card_gate" if _cg else "gate-marker-only (card_gate import failed)",
                      "cite_normalize": {"normalized": sum(1 for x in cite_log if x["action"] == "normalized"), "fetch": sum(1 for x in cite_log if x["action"] == "fetch")},
                      "by_claim": dict(collections.Counter(str(c["claim"])[:40] for c in cells))}, ensure_ascii=False))
    bad = [c for c in cells if not (isinstance(c["claim"], int) and (c["claim"] in points or c["claim"] in new_units or (c["claim"] == 0 and kind(c) in ("posture", "remedy"))))]
    if bad: raise SystemExit(f"cards_to_cells: {len(bad)} cell(s) with a non-point claim — refusing to emit")


if __name__ == "__main__":
    main(*sys.argv[1:5])
