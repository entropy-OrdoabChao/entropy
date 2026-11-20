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
S-room-closure (2026-11-13, class — BACKLOG "ROOM — run-to-closure ledger"): the ledger is APPEND-ONLY and UPSERTED, never
truncated (the old "w" open wiped r3's 70 cells on every new run, so closing units 14-20 meant hand-merging archived files).
Every row carries `run` = the relay tag that produced it; a row already in the ledger with no `run` is tagged `[hand]` so a
human insertion stays visible. Upsert key = (unit, lane, claim text); newest wins EXCEPT that a GROUNDED/RECORD row is never
overwritten by an OPEN/weaker row (no downgrade). CELLS_APPEND=0 restores the truncating path byte-identically.
usage: cards_to_cells.py <tag> <room_inputs_dir> <out_cells.jsonl> [cards_dir]"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, sys, json, glob, os
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

# ---- F2.1 (2026-11-10): ADMIN / REGULATOR ROOM lanes carry their own card form (not the Issue/Rule/Violation form).
# Each lane's card body is "<HEAD> <n>: <subject> — LABEL value — LABEL value …" (labels in caps, joined by em-dashes); the
# fields below are the labels the lane spec names. It maps labels → fields; it does not reason. A field the card does not
# carry is None (never invented). with_authority = real [corpus:…]/[official:…] tags in the body.
ADMIN_LANES = {"actor-map": "actor", "records-request": "request", "formal-demand": "demand",
               "deadline-tracker": "clock", "failure-escalation": "escalation"}
ADMIN_FIELDS = {
    "actor":      {"ROLE": "role", "REGULATOR": "regulator", "COMPLAINT CHANNEL": "complaint_channel", "RECORDS CHANNEL": "records_channel", "LADDER": "ladder", "AUTHORITY": "authority"},
    "request":    {"ROLE": "role", "REGULATOR": "regulator", "RECORDS CHANNEL": "records_channel", "STATUTE": "statute", "RULE": "rule", "CUSTODIAN": "custodian", "FEE RULE": "fee_rule", "RESPONSE CLOCK": "clock", "ON LAPSE": "on_lapse", "MAIL": "mail", "LADDER": "ladder", "AUTHORITY": "authority"},
    "demand":     {"STATUTE": "statute", "DUTY": "duty", "TRIGGER": "trigger", "CLOCK": "clock", "ON LAPSE": "on_lapse", "RECORD": "record", "MAIL": "mail"},
    "clock":      {"START": "start", "START UNDATED": "start_undated", "RULE": "rule", "DUE": "due", "MAIL": "mail", "STATUS": "clock_status"},
    "escalation": {"CLAIM": "claim_statute", "ELEMENTS": "elements", "VEHICLE": "vehicle", "FORUM": "forum", "RULE": "rule", "HANDOFF": "handoff"},
}
ADMIN_HEAD_RX = re.compile(r"\s*(ACTOR|REQUEST|DEMAND|CLOCK|ESCALATION)\s+(\d{1,2})\s*[:\-—]?\s*(.*)", re.S)
ADMIN_TAG_RX = re.compile(r"\[(corpus|official|ladder|doc):\s*([^\]]+)\]")
MIRROR_ONLY_RX = re.compile(r"MIRROR|Issue-form|Issue form", re.I)


def parse_admin(kind, finding, proposition, where="", authority=""):
    """Split an admin card into {subject, number, <fields>, authorities[], not_in_corpus[]} by its own labels.
    The card scaffold is 'CARD: <lane> | Issue n | <ADMIN BODY> | STATUS | PROPOSITION: …' — parse_cards files the body under
    where/authority (it is not an Issue-form finding), so the body is looked for across finding, where and authority."""
    cands = [x for x in (finding, where, authority, proposition) if x and ADMIN_HEAD_RX.match(x)]
    body = cands[0] if cands else (where or finding or proposition)
    hm = ADMIN_HEAD_RX.match(body or "")
    number = int(hm.group(2)) if hm else None
    rest = hm.group(3) if hm else (body or "")
    labels = ADMIN_FIELDS[kind]
    # split on " — LABEL " boundaries (label = the longest known label at that point)
    lab_rx = re.compile(r"\s[—–-]\s(" + "|".join(sorted(map(re.escape, labels), key=len, reverse=True)) + r")\b\s*[:\-]?\s*")
    parts = lab_rx.split(rest)
    out = {"kind": kind, "number": number, "subject": parts[0].strip(" —-:") or None}
    for lab, val in zip(parts[1::2], parts[2::2]):
        f = labels[lab]
        out[f] = (val or "").strip(" —-;") or None
    text = " ".join(x for x in (finding, where, authority, proposition) if x)
    auths = []
    for fam, ref in ADMIN_TAG_RX.findall(text):
        if fam in ("corpus", "official"):
            ref = ref.strip()
            q = re.search(r"\[" + fam + r":\s*" + re.escape(ref) + r"\]\s*[“\"]([^”\"]{12,})[”\"]", text)
            auths.append({"cite": "", "quote": q.group(1) if q else "", "source": ref, "family": fam, "verified": False})
    out["authorities"] = auths
    out["not_in_corpus"] = [m.group(0)[:160] for m in re.finditer(r"[A-Z][A-Z /]{2,40}:?\s*[^—]{0,80}?NOT IN (?:CORPUS|LADDER)[^—]{0,80}", text)]
    return out


def admin_verify(auths):
    """Mark an authority verified when its chunk id resolves (verify_quote ladder when a quote is carried; else chunk lookup)."""
    for a in auths:
        if a["family"] != "corpus": continue
        if a.get("quote"):
            v = verify_quote(a["quote"], "", a["source"].split("#")[-1] if "#" in a["source"] else a["source"])
            if v and "source" in v: a["verified"], a["source"], a["verify"] = True, v["source"], v["how"]; continue
        try:
            import sqlite3
            cid = a["source"].split("#")[-1]
            db = sqlite3.connect("file:~/turbovec/chunk_text.db?mode=ro", uri=True, timeout=30)
            a["verified"] = bool(db.execute("select 1 from chunk_text where chunk_id=? or chunk_id like ?", (cid, cid + "%")).fetchone())
        except Exception:
            pass
    return auths


VERDICT_REASON = {}   # F2.1: (room, axis, n) → skeptic reason text


def parse_cards(tag, cards_dir):
    cards, verdicts, records = [], {}, []
    for f in sorted(glob.glob(f"{cards_dir}/{tag}_relay_r*_cards.md")):
        room = int(re.search(r"_r(\d+)_", f).group(1)); per_axis = {}
        for l in open(f, errors="ignore").read().splitlines():
            if l.startswith("CARD:"):
                p = [x.strip() for x in l[5:].split(" | ")]
                si = next((i for i in range(len(p) - 1, 1, -1) if STATUS_RX.match(p[i].upper())), None)
                # S-unit-key-column (2026-11-16): the room now posts `CARD: <axis> | Issue <n> | <FORM> …` — the unit key is its own
                # column and the FORM (Vehicle:/Relief:/Rule:/PROPOSITION:) sits in p[2], not p[1]. The p[1]-only test below read 0
                # of 191 corpus-tagged cards as forms (7 pins from 213 cards; m1 refused: VEHICLE NOT DERIVED, RELIEF 0). Class
                # rule: the FORM is found by its LABEL in whichever column carries it; a bare unit key in p[1] stays the finding
                # (the unit pin reads 'Issue n' from the finding at the claim step). One key set with every other card reader.
                _FORM_RX = re.compile(r"^(?:(?:Issue|UNIT|Unit|Point)\s*#?\s*\d{1,3}\s*[—:.-]+\s*)?(?:Vehicle|Relief\s*\d*|Rule|PROPOSITION|GROUND\+?)\s*:", re.I)
                _KEY_RX = re.compile(r"^(?:Issue|UNIT|Unit|Point)\s*#?\s*\d{1,3}\s*$", re.I)
                if si is None and len(p) >= 3 and _KEY_RX.match(p[1]) and _FORM_RX.match(p[2]):
                    body = " | ".join(p[2:])
                    p = [p[0], p[1], "", "", "FORM", body if body.upper().startswith("PROPOSITION:") else "PROPOSITION: " + body]; si = 4
                elif si is None and len(p) >= 2 and re.match(r"(Vehicle|Relief)\b[^|]{0,120}?(—|-) ?Rule:", p[1]):
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
                # S-unit-key-column (cont.): with a STATUS scaffold the FORM column ('Vehicle: … — Rule: …' / 'Issue n — Rule: … Violation: …')
                # sits in mid and the PROPOSITION: column is the lane's one-sentence self-grade. Every downstream parser (parse_posture,
                # parse_remedy, _split_labels(VIOLATION_LABELS), _rule_parts → the authority pin) reads the FORM, so the FORM is the
                # proposition; the self-grade sentence joins `where`. Before this, the pin/vehicle/rule were read from the sentence → none.
                _fi = next((i for i, x in enumerate(mid) if _FORM_RX.match(x)), None) if _KEY_RX.match(p[1]) else None
                if _fi is not None:
                    _form = mid.pop(_fi)
                    if prop and not prop.upper().startswith(("VEHICLE", "RELIEF", "RULE")): mid.append("SELF-GRADE: " + prop)
                    prop = _form
                is_auth = lambda x: (bool(re.search(CITE_RX, x[:60])) or bool(re.match(r"\[?" + CHUNK_RX, x))) and len(x) < 400
                auth = next((x for x in reversed(mid) if is_auth(x)), "")
                where = " ; ".join(x for x in mid if x != auth)
                cards.append({"room": room, "n": n, "axis": p[0], "finding": p[1], "where": where, "authority": auth,
                              "status": p[si].split()[0].upper(), "proposition": prop, "gate": gate, "line": l})
            elif l.startswith("VERDICT:"):
                p = [x.strip() for x in l[8:].split(" | ")]
                if len(p) >= 3:
                    m = re.search(r"\d+", p[1]); verdicts[(room, p[0], int(m.group(0)) if m else -1)] = p[2].split()[0].upper()
                    VERDICT_REASON[(room, p[0], int(m.group(0)) if m else -1)] = " | ".join(p[3:])
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
    src = cid or next((ids[0] for c, ids in chunks_by_cite.items() if ids and (norm(c)[:18] and norm(c)[:18] in norm(cite) or norm(cite)[:18] in norm(c))), "")
    # a rule cited as "Phila. Civ. R. *1028(c)(1)(b)" → the forum-section source for *1028(c)
    if not src:
        rk = re.match(r"(Phila\.? ?Civ\.? ?R\.? ?\*?\d+(?:\.\d+)?(?:\([a-z0-9]+\))?|Pa\.R\.C\.P\. ?\d+(?:\([a-z0-9]+\))?)", cite)
        if rk:
            for c, ids in chunks_by_cite.items():
                if norm(c).startswith(norm(rk.group(1))[:12]): src = ids[0]; break
    return {"cite": cite, "quote": quote, "source": src}


def _split_auth_field(field):
    """'cite — "q" — chunk; cite — "q" — chunk' → the pieces (split on ';' OUTSIDE quotes; a '; ' inside a quote stays)."""
    out, buf, inq = [], [], False
    for ch in field or "":
        if ch in "“”\"": inq = not inq
        if ch == ";" and not inq: out.append("".join(buf)); buf = []; continue
        buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def authorities_of(field, cite_by_slug, chunks_by_cite):
    """S-multi-authority (po-16): the card's authority column carries EVERY authority that grounds the finding, ';'-separated
    (definition + statute/rule + treatise). The old single-dict authority_of kept only the first — 49 unit-2 cells, none with 2+
    authorities, so the treatise/definition layer the pack carried never became a cell. Returns a list (may be empty)."""
    out, seen = [], set()
    for piece in _split_auth_field(field):
        a = authority_of(piece, cite_by_slug, chunks_by_cite)
        if a and (a["cite"], a["source"]) not in seen:
            seen.add((a["cite"], a["source"])); out.append(a)
    return out


def _verify_auth(a, gate_txt):
    """the per-authority verification ladder (same as the primary authority's): gate hit → verified; else verify_quote."""
    gm = re.search(r"GATE: quote verified \(([^)]+)\)", gate_txt or "")
    a["verified"] = bool(a.get("quote")) and bool(gm) and not gm.group(1).endswith((".md", ".txt"))
    if gm and re.match(CHUNK_RX + r"$|[0-9a-f]{16,}$|usc_t\d+_s\w+$|amjur2d_\w+$", gm.group(1)) and not a.get("source"): a["source"] = gm.group(1)
    if not a["verified"] and a.get("quote"):
        v = verify_quote(a["quote"], a.get("cite") or "", a.get("source") or None)
        if v and "source" in v: a["verified"], a["source"], a["verify"] = True, v["source"], v["how"]
    return a


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
    """lane + proposition → 'violation' | 'posture' | 'remedy' (the lane table decides; the text only decides the '?' lanes).
    S-form-by-label (2026-11-16): a card that WRITES the posture form ('Vehicle: <name> — Rule: …') or the remedy form
    ('Relief <n>: …') IS that form whatever lane posted it — the 2026-11-16 room's jurisdiction lane carried the § 5505 vehicle
    cards (SUSTAINED, corpus-pinned) and the lane table filed them as violations, so m1 saw 'VEHICLE: NOT DERIVED'. The lane
    table still decides every card that does not name its form."""
    t = proposition or ""
    if re.match(r"\s*Vehicle(?:\s+\d+\s+of\s+\w+)?\s*:", t):
        return "posture"
    if re.match(r"\s*Relief\s*\d*\s*:", t):
        return "remedy"
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


# ---- S-room-closure: the run-to-closure ledger (append + upsert, no downgrade, [hand] origin)
HAND = "[hand]"                      # a ledger row with no `run` tag was inserted by a human, not by a room
LEDGER_APPEND = os.environ.get("CELLS_APPEND", "1") != "0"
STATUS_RANK = {"GROUNDED": 3, "RECORD": 3, "NOT IN CORPUS": 2, "OPEN": 1}


def _claim_text(c):
    """the row's CLAIM (the court-facing sentence), normalised — the third part of the upsert key."""
    t = c.get("proposition") or c.get("relief_item") or c.get("finding") or c.get("title") or ""
    return re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()[:160]


def ledger_key(c):
    """(unit, lane, claim) — the backlog's upsert key. unit = claim number, lane = the bot/axis."""
    return (c.get("claim"), str(c.get("bot") or c.get("lane") or "").lower(), _claim_text(c))


def _rank(c):
    return STATUS_RANK.get(str(c.get("status") or "").upper(), 0)


def merge_ledger(out_path, new_cells):
    """prior ledger rows + this run's rows -> one ledger. Order: prior rows keep their position (a replaced row is replaced
    IN PLACE), new keys append — so replaying the same cards twice writes a byte-identical file. A GROUNDED/RECORD row is
    never overwritten by a weaker one (no downgrade); the loser is counted, not dropped from history (its own run keeps it).
    S-cells-guard: the ONE exception to no-downgrade is a row the DETERMINISTIC guards failed (family-mismatch / missing
    record pointer / connected record not cited). No-downgrade exists to stop a weaker-evidence row from erasing a stronger
    one; it must not protect a row the guard has disproved, or the first run's bad GROUNDED would close the unit forever."""
    prior = []
    if LEDGER_APPEND and os.path.exists(out_path):
        for ln in open(out_path, errors="ignore"):
            ln = ln.strip()
            if not ln: continue
            try: r = json.loads(ln)
            except Exception: continue
            if isinstance(r, dict):
                r.setdefault("run", HAND)          # no run tag = a human put it there; keep it visible as [hand]
                prior.append(r)
    stats = {"prior": len(prior), "hand": sum(1 for r in prior if r.get("run") == HAND),
             "new": len(new_cells), "added": 0, "replaced": 0, "kept_no_downgrade": 0, "guard_override": 0}
    out, idx = [], {}
    for r in prior:
        k = ledger_key(r)
        if k in idx: out[idx[k]] = r                # a duplicate already in the file: the later line is the newer one
        else: idx[k] = len(out); out.append(r)
    for r in new_cells:
        k = ledger_key(r); i = idx.get(k)
        if i is None:
            idx[k] = len(out); out.append(r); stats["added"] += 1
        elif _rank(r) < _rank(out[i]) and not r.get("guard"):
            stats["kept_no_downgrade"] += 1          # GROUNDED/RECORD stands; OPEN/weaker never overwrites it
        else:
            if _rank(r) < _rank(out[i]): stats["guard_override"] += 1    # the guard disproved the stronger prior row
            out[i] = r; stats["replaced"] += 1
    return out, stats


# ---- S-cells-guard (2026-11-13, class — BACKLOG "PREFETCH/SKEPTIC: a cite is 'resolved' when the pinned chunk merely
# MENTIONS it", parts 2-4). Three deterministic refusals, every one of them a GUARDRAIL on the room's own output (the lanes
# and the skeptic still do the reasoning; this only refuses to call a card GROUNDED when the record contradicts it):
#   (2) FAMILY CHECK — the pinned chunk must BE the cite the card names (card_gate.family_check). The closure run pinned
#       24 C.F.R. § 203.431 to usc_t12_s1748b (armed-services housing) and 13 Pa.C.S. § 9308(e) to pa_cs_t7, and the
#       skeptic graded both GROUNDED. Checked twice: the cell's authority (cite ↔ source) AND every '<cite> … [corpus:<id>]'
#       attribution in the card's own text (that is where the drift is written). Mismatch ⇒ the cell FAILS, reason logged
#       to <m2>/FAMILY_CHECK.md per run.
#   (3) RECORD POINTER — a GROUNDED/RECORD cell that ASSERTS a date, a dollar amount, a docket/ECF/Control number or an
#       exhibit letter must carry a resolvable record pointer ('record.md:<line>' / 'adverse.md:<¶>' / '[doc:<case>/<note>]').
#       Pointers the card wrote elsewhere (its 'record:Point n' / 'adverse §C' pin, its [doc:] tags) are RESOLVED and filled
#       into record_facts first — the guard demands a pointer, not a particular column. 71/71 closure cells had none.
#       An authorities entry beginning QUESTION:/ANSWER:/SKEPTIC: is skeptic text that leaked through the card parser: it is
#       dropped from authorities and logged to <m2>/PARSE_DEFECTS.md with the relay id; it can never ground a cell.
#   (4) CONNECTED RECORD — when connected_record.md is in the pack and a unit's text names the connected case (slug, docket
#       alias from cases_index/case_map.json, or the [[cases/<slug>/_case]] link), the unit closes only if one of its closing
#       cells cites that record with a [doc:<slug>/…] pointer; otherwise its closing cells go OPEN with
#       gap='connected record not cited' so the closure loop re-dispatches the unit with that gap text.
# Each guard has its own kill switch (CELLS_FAMILY_CHECK / CELLS_RECORD_GUARD / CELLS_CONNECTED_GUARD = 0) restoring the
# previous output byte-identically.
GUARD_FAMILY = os.environ.get("CELLS_FAMILY_CHECK", "1") != "0"
GUARD_RECORD = os.environ.get("CELLS_RECORD_GUARD", "1") != "0"
GUARD_CONNECTED = os.environ.get("CELLS_CONNECTED_GUARD", "1") != "0"
PARSE_DEFECT_RX = re.compile(r"^\s*(QUESTION|ANSWER|SKEPTIC|VERDICT)\s*:", re.I)
UNBRACKETED_DOC_RX = re.compile(r"(?<!\[)\bdoc:\s*[\w./-]+/[^\s;|)]+")
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
ASSERT_RX = {
    "date": re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b(?:" + _MONTHS + r") \d{1,2}, \d{4}\b|\b\d{4}-\d{2}-\d{2}\b"),
    "amount": re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"),
    "docket": re.compile(r"(?:Control No\.?|ECF No\.?|Dkt\.?|Docket No\.?|Case No\.?)\s*#?\s*\d{3,}|\b\d{1,2}:\d{2}-cv-\d{3,}\b|\b\d{2}-cv-\d{3,}\b"),
    "exhibit": re.compile(r"\bEx(?:h|hibit)s?\.?\s+[A-Z]\b"),
}
POINTER_OK_RX = re.compile(r"^(record\.md:\d+|adverse\.md:(?:¶\d+|\d+)|\[doc:[^\]]+\])$")


def _ptr_ok(p):
    """a record_fact entry carries a real record pointer — a bare string, or a structured entry {'pointer': …} such as the
    contradiction cells' {pointer, source_kind, quote} (S-skeptic-contradiction). A structured pointer IS a pointer."""
    if isinstance(p, dict):
        p = p.get("pointer") or p.get("where") or ""
    return bool(POINTER_OK_RX.match(str(p).strip()))
DOC_TAG_RX = re.compile(r"\[doc:\s*([^\]]+)\]")
REC_PIN_RX = re.compile(r"record\s*:\s*(?:Point|Issue)\s*(\d{1,3})", re.I)
ADV_SEC_RX = re.compile(r"adverse(?:\.md)?\s*[§]\s*([A-Z])\b", re.I)
ADV_PARA_RX = re.compile(r"(?:adverse(?:\.md)?[^.;|]{0,24})?¶{1,2}\s*(\d{1,4})")

# ---- S-skeptic-contradiction (2026-11-14, CLASS-LEVEL): the gate's second duty — THEIR paper against THEIR paper.
# The skeptic posts `CARD: skeptic | CONTRADICTION: … | ADMISSION: "<verbatim>" [doc:<p>] ; STATEMENT: "<verbatim>" [doc:<p>] |
# ELEMENT: … | SUSTAINED | PROPOSITION: … VEHICLE: …`. The COMPREHENSION (do these two statements contradict?) is the lane's;
# everything below is the deterministic GUARDRAIL: both sides must be quoted VERBATIM and each must carry its OWN [doc:] pointer,
# or no cell is written. Doctrine behind the two source_kinds: a statement in the PLEADING is a judicial admission — "formal
# judicial admissions which waive any evidence by the adverse party on the admitted matter are conclusive on the pleader"
# [corpus:amjur2d_30_evidence#68448b486a], "A party is generally bound by allegations or admissions in his own pleadings"
# [corpus:amjur2d_1stPleading_volume_1stpleading#244b159b66]; a statement outside the pleadings is an "extrajudicial admission.
# An admission made outside court proceedings." [corpus:frb_chicago_fractional#ca3eeb0471] — evidential, impeaching, explainable.
SKEPTIC_CONTRA = os.environ.get("CELLS_SKEPTIC_CONTRA", "1") != "0"
CONTRA_RX = re.compile(r"\s*CONTRADICTION\s*:", re.I)
_ADM_RX = re.compile(r"\bADMISSION\s*:\s*(.+?)(?=\s*[;|]?\s*(?:EXTRAJUDICIAL\s+)?STATEMENT\s*:|\s*\|\s*ELEMENT\s*:|$)", re.I | re.S)
_STM_RX = re.compile(r"\b(?:EXTRAJUDICIAL\s+)?STATEMENT\s*:\s*(.+?)(?=\s*[;|]?\s*ELEMENT\s*:|\s*\|\s*(?:SUSTAINED|OPEN|REJECTED|NOT IN CORPUS)\b|$)", re.I | re.S)
_ELEM_RX = re.compile(r"\bELEMENT\s*:\s*([^|]+)", re.I)
_VEH_RX = re.compile(r"\bVEHICLE\s*:\s*(.+?)(?=\s*\|\s*[A-Z]{3,}\s*:|$)", re.I | re.S)
CONTRA_KINDS = ("judicial_admission", "opponent_writing")


def _contra_side(text):
    """(quote, pointer) a labelled side of a contradiction card carries — '' / '' when it is not quoted or not pointed."""
    q = QUOTE_RX.search(text or "")
    p = DOC_TAG_RX.search(text or "")
    return ((q.group(1).strip() if q else ""), (f"[doc:{p.group(1).strip()}]" if p else ""))


def contra_cell(card, verdict, tag, profiles):
    """(cell, why) for a skeptic CONTRADICTION card: kind=contradiction / status=RECORD, both sides quoted and pointed, the
    pleading side tagged source_kind=judicial_admission and the other opponent_writing. (None, why) when the guardrail refuses."""
    line = card.get("line") or ""
    m_a, m_s = _ADM_RX.search(line), _STM_RX.search(line)
    if not (m_a and m_s):
        return None, "no ADMISSION:/STATEMENT: labels — the card does not classify the two statements"
    a_q, a_p = _contra_side(m_a.group(1))
    s_q, s_p = _contra_side(m_s.group(1))
    if not (a_q and s_q):
        return None, "a side is not quoted verbatim" + (" (ADMISSION)" if not a_q else "") + (" (STATEMENT)" if not s_q else "")
    if not (a_p and s_p):
        return None, "a side carries no [doc:] pointer" + (" (ADMISSION)" if not a_p else "") + (" (STATEMENT)" if not s_p else "")
    if a_p == s_p:
        return None, f"both statements point at the same document {a_p} — one pointer is not a contradiction of record"
    el = _ELEM_RX.search(line)
    vh = _VEH_RX.search(card.get("proposition") or "") or _VEH_RX.search(line)
    vehicle = (vh.group(1).strip().rstrip(".") if vh else "")
    # the unit this contradiction belongs to: the card's own 'Issue <n>' hint when it carries one (same rule as a violation
    # card), else content pinning against the units' own words. A contradiction with no unit is still kept at claim 0.
    _pn = re.match(r"\s*(?:Issue|Point|Ground)\s+(\d{1,2})\b", card.get("proposition") or "", re.I) \
        or re.search(r"\b(?:Issue|Point|Ground)\s+(\d{1,2})\b", card["finding"] or "", re.I)
    pin = pin_unit(card["finding"] + " " + (card.get("proposition") or ""),
                   int(_pn.group(1)) if _pn else None, profiles)
    rf = [{"pointer": a_p, "source_kind": "judicial_admission", "quote": a_q,
           "classification": "JUDICIAL ADMISSION — in the pleading; conclusive on the pleader, the fact is withdrawn from issue"},
          {"pointer": s_p, "source_kind": "opponent_writing", "quote": s_q,
           "classification": "EXTRAJUDICIAL STATEMENT — outside the pleadings; evidential and impeaching, not conclusive"}]
    cell = {"claim": pin.get("unit") or 0, "title": card["finding"][:160], "kind": "contradiction", "form": "record",
            "status": "RECORD", "level": 6, "bot": card["axis"], "finding": card["finding"],
            "proposition": card.get("proposition") or "", "verdict": verdict, "where": card.get("where") or "",
            "element_defeated": (el.group(1).strip() if el else None),
            "vehicle": vehicle or None, "vehicle_not_in_corpus": bool(vehicle) and "NOT IN CORPUS" in vehicle.upper(),
            "authorities": [], "gap": None, "fix": None, "record_facts": rf,
            "relay": f"{tag} r{card['room']} {card['axis']}#{card['n']}",
            "text": (f"CONTRADICTION (skeptic, {card['status']}): {card['finding']}"
                     f" | JUDICIAL ADMISSION: \u201c{a_q}\u201d {a_p}"
                     f" | EXTRAJUDICIAL STATEMENT: \u201c{s_q}\u201d {s_p}"
                     + (f" | element defeated: {el.group(1).strip()}" if el else "")
                     + (f" | vehicle: {vehicle}" if vehicle else ""))}
    return cell, ""


def pack_index(inputs):
    """The pack's own anchors, so a card's record pin resolves to a LINE: record.md '## Point n' / '## <n>.' headings and
    its [doc:] blocks, adverse.md '## A.' section headings. {record_points, record_docs, adverse_secs, has_record, has_adverse}."""
    idx = {"record_points": {}, "record_docs": {}, "adverse_secs": {}, "has_record": False, "has_adverse": False}
    try:
        lines = open(f"{inputs}/record.md", encoding="utf-8", errors="ignore").read().splitlines()
        idx["has_record"] = True
        for i, l in enumerate(lines, 1):
            m = re.match(r"^#{1,4}\s*(?:Point|Issue)\s*(\d{1,3})\b", l) or re.match(r"^\*\*(\d{1,3})\.\s", l)
            if m: idx["record_points"].setdefault(int(m.group(1)), i)
            for d in DOC_TAG_RX.findall(l): idx["record_docs"].setdefault(d.strip(), i)
    except Exception:
        pass
    # S-pointer-index-verified-record (2026-11-16): verified_record.md line 1 TELLS the lanes "cite as record:<point>" and carries
    # '## Point n.' headings; the lanes obeyed ((record:Point 6) ×8 in one leg) but this index read record.md only (elements lists,
    # no Point headings) → every record:Point pin resolved to nothing → 93 of 129 verified, corpus-pinned rows demoted to OPEN
    # ("record_facts missing") → m1 refused. One key set: the file that defines the pointer form is indexed for it.
    try:
        lines = open(f"{inputs}/verified_record.md", encoding="utf-8", errors="ignore").read().splitlines()
        for i, l in enumerate(lines, 1):
            m = re.match(r"^#{1,4}\s*(?:Point|Issue)\s*(\d{1,3})\b", l) or re.match(r"^\*\*(\d{1,3})\.\s", l)
            if m and int(m.group(1)) not in idx["record_points"]:
                idx.setdefault("vr_points", {})[int(m.group(1))] = i; idx["has_record"] = True
    except Exception:
        pass
    try:
        lines = open(f"{inputs}/adverse.md", encoding="utf-8", errors="ignore").read().splitlines()
        idx["has_adverse"] = True
        for i, l in enumerate(lines, 1):
            m = re.match(r"^#{1,4}\s*([A-Z])\.\s", l)
            if m: idx["adverse_secs"].setdefault(m.group(1), i)
    except Exception:
        pass
    return idx


def record_pointers(text, idx):
    """every RESOLVABLE record pointer in `text`, in the three pointer forms. A pin that does not resolve to a real anchor
    (a point the record does not carry, an unbracketed 'doc: …') is NOT a pointer — the guard reports it as missing."""
    out, t = [], text or ""
    for d in DOC_TAG_RX.findall(t):
        p = f"[doc:{d.strip()}]"
        if p not in out: out.append(p)
    for m in re.finditer(r"\b(record\.md:\d+|adverse\.md:(?:¶\d+|\d+))\b", t):
        if m.group(1) not in out: out.append(m.group(1))
    for m in REC_PIN_RX.finditer(t):
        ln = idx["record_points"].get(int(m.group(1)))
        if ln and f"record.md:{ln}" not in out: out.append(f"record.md:{ln}")
        vl = (idx.get("vr_points") or {}).get(int(m.group(1)))     # S-pointer-index-verified-record: the pointer names ITS file
        if not ln and vl and f"[doc:verified_record.md:L{vl}]" not in out: out.append(f"[doc:verified_record.md:L{vl}]")
    for m in ADV_SEC_RX.finditer(t):
        ln = idx["adverse_secs"].get(m.group(1).upper())
        if ln and f"adverse.md:{ln}" not in out: out.append(f"adverse.md:{ln}")
    if idx["has_adverse"]:
        for m in ADV_PARA_RX.finditer(t):
            if "adverse" in t[max(0, m.start() - 40):m.start()].lower():
                p = f"adverse.md:¶{m.group(1)}"
                if p not in out: out.append(p)
    return out


def ground_plus_sources(inputs, tag, cards_dir):
    """chunk id → source key, free and exact, from the pack's and the cards' own GROUND+ lines
    ('GROUND+: <cite> | <chunk id> | "<quote>" | source=<key> [fam]') plus the pack's '### R.'/'source:' rows."""
    hint = {}
    files = [f"{inputs}/ground_pack.md"] + sorted(glob.glob(f"{cards_dir}/{tag}_relay_r*_cards.md"))
    for p in files:
        if not os.path.exists(p): continue
        for l in open(p, errors="ignore"):
            if "GROUND+" not in l: continue
            parts = [x.strip() for x in l.split("|")]
            cid = next((x for x in parts[1:2] if re.match(r"^[\w.#-]+$", x or "")), None)
            sm = re.search(r"source=(\S+)", l)
            if cid and sm:
                hint.setdefault(cid, sm.group(1))
                if "#" in cid: hint.setdefault(cid.split("#")[-1], sm.group(1))
    return hint


TEXT_CITE_RX = re.compile(r"\d+\s*(?:U\.?\s?S\.?\s?C\.?(?:A\.?)?|C\.?\s?F\.?\s?R\.?|Pa\.?\s?C\.?\s?S\.?(?:A\.?)?|P\.\s?S\.|Pa\.?\s?Code)\.?\s*§*\s*[\d.]*[a-z]?"
                          r"|Pa\.\s?R\.\s?[A-Za-z.]*\.?\s*\d+(?:\.\d+)?|Fed\.\s?R\.\s?\w+\.\s?P\.?\s*\d+")


def text_attributions(text):
    """[(cite, chunk id)] for every '… <cite> … [corpus:<id>]' attribution written in a card. The cite is the LAST one
    before the tag (within 240 chars), else the first within 160 chars after it — the way the card forms are written:
    'Rule: 24 C.F.R. § 203.431 — "…" [corpus:usc_t12_s1748b#…]'. This is where the family drift is recorded."""
    t = text or ""
    cites = [(m.start(), m.end(), re.sub(r"\s+", " ", m.group(0)).strip(" .,;")) for m in TEXT_CITE_RX.finditer(t)]
    out = []
    for tm in CORPUS_TAG_RX.finditer(t):
        cid = tm.group(1)
        before = [c for c in cites if c[1] <= tm.start() and tm.start() - c[1] <= 240]
        pick = before[-1][2] if before else next((c[2] for c in cites if c[0] >= tm.end() and c[0] - tm.end() <= 160), None)
        if pick: out.append((pick, cid))
    return out


def connected_aliases(inputs):
    """The connected case(s) this pack carries, with the tokens a card may name them by: the slug, the [[cases/<slug>/_case]]
    link, and every docket alias the case index knows (plus the docket's bare number, which is how lanes write it).
    A slug counts only if the vault actually has cases/<slug>/ (or the case index names it) — a [doc:] tag's first segment
    is otherwise just text ('output', 'intake', '<case>') and would make every unit 'name the connected case'.
    -> {slug: [tokens]} — empty when the pack has no connected_record.md."""
    p = f"{inputs}/connected_record.md"
    if not os.path.exists(p): return {}
    txt = open(p, encoding="utf-8", errors="ignore").read()
    cand = set(d.split("/")[0].strip() for d in DOC_TAG_RX.findall(txt) if "/" in d)
    cand |= set(m.group(1) for m in re.finditer(r"\[\[cases/([^/\]]+)/_case\]\]", txt))
    maps = {}
    for cm in (str(_eroot() / "cases_index/case_map.json"), str(_eroot() / "case_map.json")):
        try: maps.update(json.load(open(cm)))
        except Exception: pass
    slugs = []
    for s in sorted(x for x in cand if x and re.fullmatch(r"[A-Za-z][\w.:-]{3,60}", x)):
        if os.path.isdir(f"<home>/obsidian_sync/cases/{s}") or s in maps or any(s in k for k in maps): slugs.append(s)
    out = {}
    for s in slugs:
        toks = {s, s.replace("_", ":"), f"[[cases/{s}/_case]]"}
        dm = re.search(r"(\d{1,2}[:-]\d{2})-(cv|cr)-(\d{3,6})", s)
        if dm:
            toks |= {f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}", f"{dm.group(2)}-{dm.group(3)}", dm.group(3), dm.group(3).lstrip("0")}
        for k, v in maps.items():
            blob = json.dumps(v) if not isinstance(v, str) else v
            if s in k or s in blob or (dm and dm.group(3) in k):
                for a in re.findall(r"\d{1,2}:\d{2}-cv-\d{3,6}|\d{2}-cv-\d{3,6}", k + " " + blob): toks.add(a)
        out[s] = sorted(t for t in toks if t and len(str(t)) >= 4)
    return out


def _cell_text(c):
    return " ".join(str(c.get(k) or "") for k in ("title", "finding", "proposition", "where", "rule_verbatim", "record_fact")) + \
           " " + " ".join(f"{a.get('cite') or ''} {a.get('quote') or ''} {a.get('source') or ''}" for a in (c.get("authorities") or []))


def _fail(c, reason, tagname):
    """refuse the cell: never GROUNDED/RECORD on a guard failure; the reason is the gap the closure loop re-dispatches with."""
    c["guard"] = sorted(set((c.get("guard") or []) + [tagname]))
    c["gap"] = "; ".join(x for x in [c.get("gap"), reason] if x)
    if str(c.get("status") or "").upper() in ("GROUNDED", "RECORD"): c["status"] = "OPEN"


def guard_cells(cells, inputs, out_path, tag, cards_dir, uprof=None):
    """Run guards (2)(3)(4) over this run's cells. Mutates the cells, writes the three logs next to the ledger, returns stats."""
    m2dir = os.path.dirname(os.path.abspath(out_path)) or "."
    hint = ground_plus_sources(inputs, tag, cards_dir)
    cpath = os.path.join(m2dir, "_family_cache.json")
    try: cache = json.load(open(cpath))
    except Exception: cache = {}
    idx = pack_index(inputs)
    conn = connected_aliases(inputs) if GUARD_CONNECTED else {}
    pre_status = {id(c): str(c.get("status") or "").upper() for c in cells}   # guard (4) judges the unit's CLOSING cells as the
    fam_rows, defects, qg_rows, conn_rows = [], [], [], []                   # room wrote them, so a cell already demoted by
    #                                                                          guard (2)/(3) still gets the connected gap text
    st: dict = {"authorities_checked": 0, "family_mismatch": 0, "family_unchecked": 0, "parse_defects": 0,
          "pointer_filled": 0, "quote_guard_demoted": 0, "connected_units_open": 0, "cells_failed": 0}

    for c in cells:
        relay = c.get("relay") or ""
        auths = c.get("authorities") or []
        # ---- (3a) parse defects: skeptic/Q&A text that leaked into the authority column is not an authority
        keep = []
        for a in auths:
            blob = f"{a.get('cite') or ''} {a.get('quote') or ''}".strip()
            if GUARD_RECORD and (PARSE_DEFECT_RX.match(a.get("cite") or "") or PARSE_DEFECT_RX.match(blob)):
                defects.append({"relay": relay, "lane": c.get("bot"), "claim": c.get("claim"), "defect": "skeptic/Q&A text in authorities",
                                "text": blob[:220]}); st["parse_defects"] += 1
                continue
            keep.append(a)
        if len(keep) != len(auths):
            c["authorities"] = keep
            if not keep: _fail(c, "authorities dropped as parse defect (QUESTION:/ANSWER:/SKEPTIC: leak)", "parse-defect")
        for m in UNBRACKETED_DOC_RX.finditer(_cell_text(c)):      # '(doc: case/file)' is not a [doc:…] pointer — log, never count
            defects.append({"relay": relay, "lane": c.get("bot"), "claim": c.get("claim"),
                            "defect": "record pointer written unbracketed (doc: … instead of [doc: …])", "text": m.group(0)[:160]})
            st["parse_defects"] += 1
            break
        # ---- (2) family check: the cell's authority, then every attribution written in the card text
        if GUARD_FAMILY:
            pairs = [((a.get("cite") or ""), (a.get("source") or a.get("chunk_id") or ""), a) for a in (c.get("authorities") or [])]
            pairs += [(cite, cid, None) for cite, cid in text_attributions(_cell_text(c))]
            seen = set()
            for cite, src, a in pairs:
                if not cite or not src: continue
                k = (cite, src)
                if k in seen: continue
                seen.add(k)
                key = _cg.chunk_source(src, hint, cache) if (_cg and hasattr(_cg, "chunk_source")) else src
                v, why = _cg.family_check(cite, key) if (_cg and hasattr(_cg, "family_check")) else ("unchecked", "card_gate has no family_check")
                st["authorities_checked"] += 1
                if v == "unchecked": st["family_unchecked"] += 1
                if v != "mismatch": continue
                st["family_mismatch"] += 1
                if a is not None: a["verified"], a["family_check"] = False, why
                fam_rows.append({"relay": relay, "lane": c.get("bot"), "claim": c.get("claim"), "cite": cite, "chunk": src,
                                 "source_key": key or "(unresolved)", "why": why})
                _fail(c, why, "family-mismatch")
        # ---- (3b) record pointer for an asserted date / amount / docket number / exhibit letter
        if GUARD_RECORD:
            text = _cell_text(c)
            asserts = [k for k, rx in ASSERT_RX.items() if rx.search(text)]
            if asserts:
                have = [p for p in (c.get("record_facts") or []) if _ptr_ok(p)]
                if not have:
                    found = record_pointers(" ".join(str(x) for x in (c.get("record_facts") or [])) + " " + text, idx)
                    if found:
                        c["record_facts"] = found + [x for x in (c.get("record_facts") or []) if x]
                        have = found; st["pointer_filled"] += 1
                if not have and str(c.get("status") or "").upper() in ("GROUNDED", "RECORD"):
                    st["quote_guard_demoted"] += 1
                    qg_rows.append({"relay": relay, "lane": c.get("bot"), "claim": c.get("claim"), "asserts": asserts,
                                    "claim_text": (c.get("proposition") or c.get("title") or "")[:160]})
                    _fail(c, "record_facts missing", "record-pointer")
    # ---- (4) connected record: a unit whose text names the connected case must cite that record in a closing cell
    if conn:
        by_unit = {}
        for c in cells:
            if isinstance(c.get("claim"), int): by_unit.setdefault(c["claim"], []).append(c)
        for unit, rows in sorted(by_unit.items()):
            utext = " ".join(_cell_text(c) for c in rows)
            if uprof and unit in uprof: utext += " " + str(uprof[unit].get("title") or "")
            named = [s for s, toks in conn.items() if any(re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", utext) for t in toks)]
            if not named: continue
            closing = [c for c in rows if pre_status.get(id(c)) in ("GROUNDED", "RECORD")]
            cited = [c for c in closing if any(re.match(r"\[doc:\s*(" + "|".join(re.escape(s) for s in named) + r")/", str(p).strip())
                                               for p in (c.get("record_facts") or []))]
            if closing and not cited:
                for c in closing: _fail(c, "connected record not cited", "connected-record")
                st["connected_units_open"] += 1
                conn_rows.append({"unit": unit, "cases": named, "closing_cells": len(closing)})
    st["cells_failed"] = sum(1 for c in cells if c.get("guard"))
    # ---- the three logs (per run, appended — the ledger's own history is append-only too)
    def _log(name, title, rows, fmt):
        p = os.path.join(m2dir, name)
        fresh = not os.path.exists(p) or os.path.getsize(p) == 0
        with open(p, "a", encoding="utf-8") as f:
            if fresh: f.write(title + "\n")
            f.write(f"\n## run {tag} — {len(rows)} finding(s)\n")
            for r in rows: f.write(fmt(r) + "\n")
            if not rows: f.write("(none)\n")
        return p
    _log("FAMILY_CHECK.md", "# FAMILY CHECK — cite ↔ pinned chunk, per run (cards_to_cells guard 2). A row here is a card that\n"
                            "named one authority and attached another's chunk; it is NOT grounded.",
         fam_rows, lambda r: f"- `{r['relay']}` {r['lane']} unit {r['claim']}: {r['why']} (chunk `{r['chunk']}` → source `{r['source_key']}`)")
    _log("PARSE_DEFECTS.md", "# PARSE DEFECTS — room text that leaked into a machine field (cards_to_cells guard 3). Dropped, never grounded.",
         defects, lambda r: f"- `{r['relay']}` {r['lane']} unit {r['claim']}: {r['defect']} — {r['text']}")
    _log("QUOTE_GUARD.md", "# QUOTE GUARD — GROUNDED/RECORD cells demoted to OPEN for asserting a date / amount / docket number /\n"
                           "exhibit letter with no resolvable record pointer (cards_to_cells guard 3). COVERAGE.md counts these as\n"
                           "'quote-guard demoted'.",
         qg_rows + [{"relay": "-", "lane": "connected-record", "claim": r["unit"], "asserts": r["cases"], "claim_text": f"{r['closing_cells']} closing cell(s) demoted: connected record not cited"} for r in conn_rows],
         lambda r: f"- `{r['relay']}` {r['lane']} unit {r['claim']}: asserts {','.join(str(x) for x in r['asserts'])} — {r['claim_text']}")
    try: json.dump(cache, open(cpath, "w"))
    except Exception: pass
    st["connected_cases"] = sorted(conn)
    st["logs"] = ["FAMILY_CHECK.md", "PARSE_DEFECTS.md", "QUOTE_GUARD.md"]
    st["family_rows"], st["parse_rows"], st["quote_rows"], st["connected_rows"] = fam_rows, defects, qg_rows, conn_rows
    return st


# ---- S-gap-second-pass (2026-11-14, _sandbox_prompt_prescreen; BACKLOG 2026-11-03 22:35 "m2 GAP second pass — re-ask only when
# the pin-path pull finds the named cite") -------------------------------------------------------------------------------------
# MEASURED (bisect 11-03, local-gpu seq8 w8, the same 72 cells): the unconditional second pass fired on 54 of 72 cells and took the
# model wall from 295 s to 591 s for GROUNDED 18 vs 16 and cites 63/73 vs 69/74 — it bought two closures and LOST six verified
# cites. The default was flipped OFF. But OFF is not the fix either: the re-ask is right exactly when the deterministic pull DID
# produce the cite the bot said was missing.
# CLASS RULE (the user's NOT-IN-CORPUS lock): a cell that says a named authority is absent is re-asked ONLY on EVIDENCE that the
# authority is now in hand — and in this repo "in hand" has ONE meaning, room_prefetch's PIN RULE: a section/rule cite is
# resolved only on an exact_header pin (pipeline/cite_family.header_matches), never on a chunk that merely MENTIONS it
# (room_prefetch.py:312). So the gate reads the RECEIPT, <inputs>/prefetch.json, which already records pin_kind per cite:
#     pin_kind == "exact_header"  (in `ground`, or a `pulls` record whose re_pin_kind is exact_header)
#         -> the pull produced the section itself: the unit is RE-DISPATCHED (the re-queue leg re-asks it with that row pinned)
#     pin_kind == "mention-only" / absent from the receipt / an ERROR entry
#         -> NO re-dispatch. The unit stays OPEN with gap='cite not in corpus after pull' and the cite goes to the PULL LIST
#            (COVERAGE.md's pull list / the fetch lane's work list) — a real NOT IN CORPUS after the pull, not a silent drop and
#            not another model call that cannot possibly see the words.
# The gate is DETERMINISTIC (it reads a receipt; it never calls a model) and it only ANNOTATES: the re-dispatch itself is the
# chain's existing re-queue stage, which now takes its unit list from SECOND_PASS.json instead of re-asking every zero-row unit.
# fired/skipped counts ride in the run summary and in SECOND_PASS.md next to the ledger. ROOM_SECOND_PASS_GATE=0 restores the
# previous output byte-identically (no gap text, no files, no summary key).
SECOND_PASS_GATE = os.environ.get("ROOM_SECOND_PASS_GATE", "1") not in ("0", "", "false")
SECOND_PASS_STATUSES = ("OPEN", "NOT IN CORPUS")          # the room's GAP: a cell whose authority did not verify
GAP_NOT_IN_CORPUS = "cite not in corpus after pull"
EXACT_PIN = "exact_header"
_SP_CITE_RX = re.compile(
    r"\d{1,2}\s+U\.?\s?S\.?\s?C\.?(?:A\.?)?\s*§*\s*\d+[a-zA-Z]?(?:[-–—]\d+[a-zA-Z]?)?(?:\([a-zA-Z0-9]{1,4}\))*"
    r"|\d{1,2}\s+C\.?\s?F\.?\s?R\.?\s*§*\s*\d{1,4}\.\d+[A-Za-z]?(?:\([a-zA-Z0-9]{1,4}\))*"
    r"|\d{1,2}\s+Pa\.?\s*C\.?\s*S\.?(?:A\.?)?\s*§*\s*\d+(?:\.\d+)?(?:\([a-zA-Z0-9]{1,4}\))*"
    r"|\d{1,3}\s+P\.?\s?S\.?\s*§+\s*\d+(?:[-.]\d+)*"
    r"|\d{1,3}\s+Pa\.?\s*Code\s*§+\s*\d+(?:\.\d+)?"
    r"|Pa\.\s*R\.\s*(?:Civ|C|A|E|J\.?A|Crim)\.\s*P\.\s*\d{1,4}(?:\.\d+)?(?:\s*\([a-zA-Z0-9]{1,4}\))*"
    r"|Fed\.?\s*R\.?\s*(?:Civ|App|Evid|Crim|Bankr)\.?\s*P?\.?\s*\d{1,4}(?:\.\d+)?(?:\s*\([a-zA-Z0-9]{1,4}\))*")
_SP_CF = None


def _sp_cite_family():
    """pipeline/cite_family.py — LIVE, promoted (never forked). None when it cannot be imported: the gate then keys cites by
    their normalised text, which is weaker but still deterministic (and logged in the receipt match reason)."""
    global _SP_CF
    if _SP_CF is None:
        _SP_CF = False
        for root in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     os.environ.get("BEEZLE_CODE_ROOT", ""), str(Path.home() / "beezle"), str(_eroot())):
            if not root or not os.path.isdir(os.path.join(root, "pipeline")): continue
            try:
                if root not in sys.path: sys.path.insert(0, root)
                from pipeline import cite_family as CF
                _SP_CF = CF; break
            except Exception: continue
    return _SP_CF or None


def sp_key(cite):
    """'24 C.F.R. § 203.431(a)' -> 'cfr:24:203.431' (the section, subsections dropped) via pipeline/cite_family.parse."""
    c = re.sub(r"\s+", " ", str(cite or "")).strip(" ,;.")
    if not c: return ""
    CF = _sp_cite_family()
    if CF is not None:
        try:
            p = CF.parse(c)
            if p.get("family") not in ("unknown", "", None) and (p.get("section") or p.get("number")):
                return f"{p['family']}:{p.get('title', '')}:{str(p.get('section') or p.get('number')).lower()}"
        except Exception: pass
    return "text:" + re.sub(r"[^a-z0-9.]", "", c.lower())


def sp_cites(text):
    """{key: cite as written} for every statute/regulation/rule cite in `text` (first written form wins)."""
    out = {}
    for m in _SP_CITE_RX.finditer(str(text or "")):
        disp = re.sub(r"\s+", " ", m.group(0)).strip(" ,;.")
        k = sp_key(disp)
        if k and k not in out: out[k] = disp
    return out


def prefetch_pins(inputs):
    """{cite key: {pin_kind, cite, chunk_id, via_pull, pull_target, status}} read from <inputs>/prefetch.json — the receipt
    room_prefetch writes (its own pin rule, its own pull records). {} when there is no receipt (then NOTHING re-dispatches:
    no receipt = no evidence)."""
    p = os.path.join(inputs, "prefetch.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    pulled = {}
    for pr in (d.get("pulls") or []):
        k = sp_key(pr.get("cite") or "")
        if k: pulled[k] = pr
    out = {}
    for bucket in ("ground", "mention", "missing"):
        for rec in (d.get(bucket) or []):
            k = sp_key(rec.get("cite") or rec.get("request") or "")
            if not k: continue
            pr = pulled.get(k) or rec.get("pulled") or {}
            pin = str(rec.get("pin_kind") or "none")
            if pin != EXACT_PIN and str(pr.get("re_pin_kind") or "") == EXACT_PIN and str(pr.get("re_resolved") or "") == "FOUND":
                pin = EXACT_PIN                                   # the one-section pull re-resolved it on its own heading
            cur = out.get(k)
            if cur and cur["pin_kind"] == EXACT_PIN: continue     # an exact pin is never downgraded by a later bucket
            out[k] = {"pin_kind": pin, "cite": rec.get("cite") or "", "chunk_id": rec.get("chunk_id") or "",
                      "status": rec.get("status") or "", "bucket": bucket,
                      "via_pull": bool(pr), "pull_target": rec.get("pull_target") or pr.get("target") or ""}
    return out


def sp_named_cites(cell):
    """The authorities THIS cell names in its own words (gap / finding / proposition / title / its authority cites) minus the
    ones that already verified for it — the same 'what the answer named' rule as the m2 runner's second_pass_cites."""
    parts = [str(cell.get(k) or "") for k in ("gap", "finding", "proposition", "title", "rule_verbatim", "fix")]
    parts += [str(a.get("cite") or "") for a in (cell.get("authorities") or []) if isinstance(a, dict)]
    have = {sp_key(a.get("cite") or "") for a in (cell.get("authorities") or [])
            if isinstance(a, dict) and a.get("verified")}
    return {k: v for k, v in sp_cites("\n".join(parts)).items() if k not in have}


def second_pass_gate(cells, inputs, m2dir=None, tag=""):
    """Annotate every GAP cell with the pin-gated second-pass decision; return the stats + the lists.

    fired   = [{unit, cite, pin_kind, chunk_id, via_pull, relay}]  -> the re-queue leg's unit list (SECOND_PASS.json)
    skipped = [{unit, cite, pin_kind, why, relay}]                 -> stayed OPEN, gap='cite not in corpus after pull'
    pull_list = [{cite, pull_target, units}]                       -> COVERAGE.md's pull list / the fetch lane's work list
    """
    stat = {"gate": "on", "cells_seen": 0, "cells_with_cite": 0, "fired": 0, "skipped": 0,
            "units_redispatch": [], "receipt": bool(0), "pins": 0, "exact_pins": 0,
            "fired_rows": [], "skipped_rows": [], "pull_list": []}
    if not SECOND_PASS_GATE:
        return {**stat, "gate": "off"}
    pins = prefetch_pins(inputs)
    stat["receipt"] = bool(pins); stat["pins"] = len(pins)
    stat["exact_pins"] = sum(1 for v in pins.values() if v["pin_kind"] == EXACT_PIN)
    pull_by_cite = {}
    for c in cells:
        if str(c.get("status") or "").upper() not in SECOND_PASS_STATUSES: continue
        stat["cells_seen"] += 1
        named = sp_named_cites(c)
        if not named: continue
        stat["cells_with_cite"] += 1
        unit = c.get("claim")
        hit = next(((k, v) for k, v in named.items() if (pins.get(k) or {}).get("pin_kind") == EXACT_PIN), None)
        if hit:
            k, disp = hit[0], hit[1]
            pin = pins[k]
            c["second_pass"] = {"redispatch": True, "cite": disp, "pin_kind": EXACT_PIN, "chunk_id": pin["chunk_id"],
                               "via_pull": pin["via_pull"], "why": "pin-path pull produced the named section's own heading"}
            stat["fired"] += 1
            stat["fired_rows"].append({"unit": unit, "cite": disp, "pin_kind": EXACT_PIN, "chunk_id": pin["chunk_id"],
                                       "via_pull": pin["via_pull"], "relay": c.get("relay") or ""})
            if isinstance(unit, int) and unit and unit not in stat["units_redispatch"]: stat["units_redispatch"].append(unit)
            continue
        worst = [(k, disp, (pins.get(k) or {}).get("pin_kind") or "not in receipt") for k, disp in named.items()]
        c["gap"] = c.get("gap") or GAP_NOT_IN_CORPUS
        c["second_pass"] = {"redispatch": False, "cites": [d for _, d, _ in worst],
                            "pin_kinds": {d: p for _, d, p in worst},
                            "why": "no exact_header pin for any named cite after the pull — NOT IN CORPUS for this run"}
        stat["skipped"] += 1
        for k, disp, pin in worst:
            stat["skipped_rows"].append({"unit": unit, "cite": disp, "pin_kind": pin, "relay": c.get("relay") or "",
                                         "why": GAP_NOT_IN_CORPUS})
            e = pull_by_cite.setdefault(disp, {"cite": disp, "pin_kind": pin,
                                               "pull_target": (pins.get(k) or {}).get("pull_target") or "", "units": []})
            if unit not in e["units"]: e["units"].append(unit)
    stat["pull_list"] = sorted(pull_by_cite.values(), key=lambda e: e["cite"])
    stat["units_redispatch"] = sorted(stat["units_redispatch"])
    if m2dir:
        try:
            json.dump({"run": tag, "units": stat["units_redispatch"], "fired": stat["fired_rows"],
                       "skipped": stat["skipped_rows"], "pull_list": stat["pull_list"],
                       "receipt": stat["receipt"], "pins": stat["pins"], "exact_pins": stat["exact_pins"]},
                      open(os.path.join(m2dir, "SECOND_PASS.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
            p = os.path.join(m2dir, "SECOND_PASS.md")
            fresh = not os.path.exists(p) or os.path.getsize(p) == 0
            with open(p, "a", encoding="utf-8") as f:
                if fresh:
                    f.write("# GAP SECOND PASS — a GAP cell is re-dispatched ONLY when room_prefetch's pull produced an\n"
                            "# exact_header pin for the cite the cell names (prefetch.json pin_kind). mention-only / absent =\n"
                            "# the unit stays OPEN with gap 'cite not in corpus after pull' and the cite goes to the PULL LIST.\n")
                f.write(f"\n## run {tag} — fired {stat['fired']} / skipped {stat['skipped']} "
                        f"(receipt pins {stat['pins']}, exact {stat['exact_pins']})\n")
                for r in stat["fired_rows"]:
                    f.write(f"- FIRED unit {r['unit']}: {r['cite']} pin=exact_header chunk `{r['chunk_id']}` "
                            f"via_pull={r['via_pull']} ({r['relay']})\n")
                for r in stat["skipped_rows"]:
                    f.write(f"- SKIPPED unit {r['unit']}: {r['cite']} pin={r['pin_kind']} — {r['why']} ({r['relay']})\n")
                if not stat["fired_rows"] and not stat["skipped_rows"]: f.write("(no GAP cell named a cite)\n")
            if stat["pull_list"]:
                with open(os.path.join(m2dir, "PULL_LIST.md"), "a", encoding="utf-8") as f:
                    f.write(f"\n## run {tag} — {len(stat['pull_list'])} cite(s) NOT IN CORPUS after the pull "
                            "(COVERAGE.md pull list; the fetch lane's work list)\n")
                    for e in stat["pull_list"]:
                        f.write(f"- {e['cite']} | pin={e['pin_kind']} | pull={e['pull_target'] or '-'} | units {e['units']}\n")
        except Exception as e:
            sys.stderr.write(f"SECOND PASS GATE: log write failed ({type(e).__name__} {str(e)[:80]}) — stats still returned\n")
    return stat


def main(tag, inputs, out_path, cards_dir=str(_eroot() / "output/research/rooms_ab")):
    cards, verdicts, records = parse_cards(tag, cards_dir)
    cite_by_slug, chunks_by_cite = ground_table(inputs); G = grounds(inputs); points = _point_heads(inputs)
    UPROF = unit_profiles(inputs)                                        # S-card-unit-pin: the units' own words (ROOM_UNITS_MD else verified_record.md)
    for _k, _p in UPROF.items(): points.setdefault(_k, _p["title"])     # a re-queue leg's record lists only its units; the pin may name any unit
    cells, side, contradictions, new_units, open_lanes, pin_log = [], [], [], {}, [], []
    vreason = VERDICT_REASON
    for c in cards:
        v = verdicts.get((c["room"], c["axis"], c["n"]))
        ok = v == "SUSTAINED" or (v is None and c["status"] == "SUSTAINED" and "GATE: quote NOT" not in c["gate"] and "carries no quote" not in c["gate"])
        # a form-only posture/remedy card (status FORM) carries no self-grade and — skeptic silent — no verdict; it is admitted to the
        # cell stage where grounded_status() verifies its verbatim rule against the corpus / official rows. The verifier, not a status
        # word, decides GROUNDED vs OPEN. (r2c: 100 such cards were set aside as "verdict OPEN" with skeptic SILENT = posture 0.)
        ok = ok or (v is None and c["status"] == "FORM")
        why = None
        if c["axis"] in META_AXES:
            why = "skeptic lane (grades, never a cell)"
            # S-skeptic-contradiction (2026-11-14, class): the gate's SECOND duty is THEIR paper against THEIR paper, and every
            # skeptic card was dropped here, so the opposing party's own inconsistency — the strongest material in any matter —
            # reached no cell and no leg of the coverage audit. A skeptic CARD whose finding starts 'CONTRADICTION:' and which
            # carries TWO distinct [doc:] pointers IS a record fact (one statement is a JUDICIAL ADMISSION conclusive on the
            # pleader, the other an EXTRAJUDICIAL STATEMENT: Am Jur 2d Evidence § 1099 / Pleading § 201, Black's), so it is
            # emitted as kind=contradiction / status=RECORD exactly like the proof lane's record facts: cells_to_stub acts on
            # GROUNDED rows only, so stubs are untouched, and no spec cites a record_fact as authority. One pointer, no pointer
            # or verdict REJECTED => no cell (an unpointed contradiction is an assertion, not a record fact). The LLM does the
            # comprehension; this parser only keeps score. CELLS_SKEPTIC_CONTRA=0 restores the old output byte-identically.
            if SKEPTIC_CONTRA and (v or "").upper() != "REJECTED" and CONTRA_RX.match(c["finding"] or ""):
                _cell, _why = contra_cell(c, v, tag, UPROF)
                if _cell: cells.append(_cell); why = "skeptic CONTRADICTION → record cell (status RECORD)"
                else: why = f"skeptic CONTRADICTION not a record fact ({_why})"
        elif c["axis"] in PROOF_AXES:
            why = "proof lane → sidecar.contradictions"; contradictions.append({"finding": c["finding"], "where": c["where"], "status": c["status"], "verdict": v, "proposition": c["proposition"], "card": c["line"][:400]})
            # S-proof-facts-as-cells (2026-11-13, class): a proof CONTRADICTION is a RECORD FACT the drafter may plead (a pleading's
            # specific denial / fact in avoidance IS a record fact — Pa.R.C.P. 1029(b)); it was invisible to m1, which reads cells
            # only, so ¶123/¶124 of a reply fell to 'deemed denied' while the decisive fact (their own Ex. A) sat in the sidecar.
            # Emitted IN ADDITION to the sidecar (unchanged) as kind=record_fact / status=RECORD: cells_to_stub acts on GROUNDED
            # rows only, so stubs are untouched; m1 specs that plead record facts (reply) see it, motion/brief specs never cite
            # a record_fact as authority. Verdict REJECTED is excluded. CELLS_PROOF_FACTS=0 restores the old output byte-identically.
            if os.environ.get("CELLS_PROOF_FACTS", "1") == "1" and (v or "").upper() != "REJECTED" and c["finding"]:
                _pm = re.search(r"¶{1,2}\s*(\d{1,4})", c["finding"] + " " + (c["where"] or ""))
                _adv_n = int(_pm.group(1)) if _pm else None
                _pin = pin_unit(c["finding"] + " " + c["proposition"], None, UPROF)
                cells.append({"claim": _pin.get("unit") or 0, "title": c["finding"][:160], "kind": "record_fact", "form": "record", "status": "RECORD", "level": 6,
                              "bot": c["axis"], "finding": c["finding"], "proposition": c["proposition"], "verdict": v, "adverse_para": _adv_n,
                              "where": c["where"], "authorities": [], "gap": None, "fix": None, "record_facts": [c["where"]] if c["where"] else [],
                              "relay": f"{tag} r{c['room']} {c['axis']}#{c['n']}",
                              "text": f"RECORD FACT (proof, {c['status']}): {c['finding']} | where: {c['where']}" + (f" | adverse ¶{_adv_n}" if _adv_n else "")})
        elif META_RX.search(c["finding"]) or META_RX.search(c["proposition"]): why = "meta/self-check"
        elif c["axis"] in ADMIN_LANES and not ok and v == "OPEN" and MIRROR_ONLY_RX.search(vreason.get((c["room"], c["axis"], c["n"]), "")):
            why = None                                    # F2.1: a MIRROR/Issue-form objection does not apply to an admin-form card; the verifier decides
        elif not ok: why = f"verdict {v or c['status']}"
        elif not c["proposition"] and c["axis"] not in ADMIN_LANES: why = "no PROPOSITION"
        if why:
            side.append({"why": why, "card": c["line"][:300]})
            if why.startswith("verdict"):
                pm = re.match(r"\s*(?:Issue|Point|Ground|Relief)\s+(\d{1,2})\b", c["proposition"])
                open_lanes.append({"lane": c["axis"], "point": int(pm.group(1)) if pm else None, "verdict": v or c["status"], "relay": f"{tag} r{c['room']} {c['axis']}#{c['n']}"})
            continue
        relay = f"{tag} r{c['room']} {c['axis']}#{c['n']}"
        # ---- F2.1 admin row: actor / request / demand / clock / escalation — the admin room's own forms (never the Issue form)
        if c["axis"] in ADMIN_LANES:
            kind_a = ADMIN_LANES[c["axis"]]
            p = parse_admin(kind_a, c["finding"], c["proposition"], c["where"], c["authority"])
            auth = admin_verify(p.pop("authorities"))
            n_a = p["number"] if (p["number"] in points or p["number"] in UPROF) else 0
            status = "GROUNDED" if any(x["verified"] for x in auth) else ("NOT IN CORPUS" if p["not_in_corpus"] and not auth else "OPEN")
            cells.append({"claim": n_a, "title": (p["subject"] or c["proposition"] or c["finding"])[:160], "kind": kind_a, "form": "admin", "status": status, "level": 6,
                          "bot": c["axis"], "finding": c["finding"], "proposition": c["proposition"], "verdict": v,
                          **{k: val for k, val in p.items() if k not in ("kind", "number", "subject")},
                          "authorities": auth, "gap": None, "fix": None, "record_facts": [], "relay": relay})
            continue
        form = classify_form(c["axis"], c["proposition"])
        _auths = authorities_of(c["authority"], cite_by_slug, chunks_by_cite)
        a = _auths[0] if _auths else None
        _extra = [_verify_auth(x, c["gate"]) for x in _auths[1:]]   # S-multi-authority: the 2nd..nth authorities, each verified
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
                      "authorities": ([a] if a else []) + _extra, "gap": None, "fix": None,
                      "record_facts": [c["finding"] + (f" ({c['where']})" if c["where"] else "")] if is_face else [],
                      "relay": relay, **({"new_unit": True, "kind": "violation"} if new else {})})
    nu_path = _new_units_md(inputs, list(new_units.values())) if new_units else None
    gst = guard_cells(cells, inputs, out_path, tag, cards_dir, UPROF)      # S-cells-guard: family check + record pointer + connected record
    cite_log = normalize_cells(cells)                                   # S-cite-drift-normalize: room cells carry the corpus pin
    sp = second_pass_gate(cells, inputs, os.path.dirname(os.path.abspath(out_path)) or ".", tag)   # S-gap-second-pass: pin-gated
    for cell in cells: cell.setdefault("run", tag)                      # S-room-closure: every row names the run that produced it
    ledger, lstats = merge_ledger(out_path, cells)                      # append + upsert (no downgrade); CELLS_APPEND=0 = old truncate
    with open(out_path, "w") as f:
        for cell in ledger: f.write(json.dumps(cell, ensure_ascii=False) + "\n")
    json.dump({"set_aside": side, "records": records, "contradictions": contradictions, "open_by_lane": open_lanes,
               "new_units": sorted(new_units), "new_units_md": nu_path, "cite_normalize": cite_log, "unit_pins": pin_log,
               "run": tag, "ledger": lstats, "guards": gst, "second_pass": sp},
              open(out_path.replace(".jsonl", "_sidecar.json"), "w"), indent=1, ensure_ascii=False)
    import collections
    kind = lambda c: c.get("kind", "violation")
    print(json.dumps({"cards": len(cards), "cells": len(cells),
                      "ledger": {**lstats, "rows": len(ledger), "path": out_path, "append": LEDGER_APPEND},
                      "by_kind": dict(collections.Counter(kind(c) for c in cells)),
                      "with_authority": sum(1 for c in cells if c["authorities"]),
                      "grounded": {k: sum(1 for c in cells if kind(c) == k and c["status"] == "GROUNDED") for k in ("posture", "remedy") + tuple(ADMIN_LANES.values()) if any(kind(c) == k for c in cells)},
                      "set_aside": dict(collections.Counter(s["why"] for s in side)),
                      "open_by_lane": [f"{o['lane']}#{o['point']}:{o['verdict']}" for o in open_lanes],
                      "new_units": sorted(new_units), "contradictions": len(contradictions),
                      "unit_pin": {"methods": dict(collections.Counter(p["method"] for p in pin_log)), "units_with_rows": len({c["claim"] for c in cells if isinstance(c["claim"], int) and c["claim"] in UPROF}), "units": len(UPROF),
                                   "number_overruled": sum(1 for p in pin_log if p["number"] in UPROF and p["unit"] != p["number"])},
                      "verifier": "card_gate" if _cg else "gate-marker-only (card_gate import failed)",
                      "guards": {k: gst[k] for k in ("authorities_checked", "family_mismatch", "family_unchecked", "parse_defects",
                                                     "pointer_filled", "quote_guard_demoted", "connected_units_open", "cells_failed")},
                      "cite_normalize": {"normalized": sum(1 for x in cite_log if x["action"] == "normalized"), "fetch": sum(1 for x in cite_log if x["action"] == "fetch")},
                      "second_pass": {k: sp[k] for k in ("gate", "cells_seen", "cells_with_cite", "fired", "skipped", "receipt", "pins", "exact_pins")} |
                                     {"units_redispatch": sp["units_redispatch"], "pull_list": len(sp["pull_list"])},
                      "by_claim": dict(collections.Counter(str(c["claim"])[:40] for c in cells))}, ensure_ascii=False))
    bad = [c for c in cells if not (isinstance(c["claim"], int) and (c["claim"] in points or c["claim"] in new_units or (c["claim"] == 0 and kind(c) in ("posture", "remedy") + tuple(ADMIN_LANES.values()))))]
    if bad: raise SystemExit(f"cards_to_cells: {len(bad)} cell(s) with a non-point claim — refusing to emit")


if __name__ == "__main__":
    main(*sys.argv[1:5])
