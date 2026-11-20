#!/usr/bin/env python3
"""soul_repair — relay-room ledger → Am Jur-anchored SKILL.md REPAIR PROPOSALS per lane (never auto-applied).

S-amjur-soul-repair (2026-10-24). Deterministic: no LLM. Input = a relay log (`rooms_ab_<tag>_relay.log`) + the
cards_to_cells summary (JSON or `auto` = re-derived here with the SAME verdict lookup cards_to_cells uses, read-only) +
the Am Jur axis audit (`output/audit/m2_amjur_axis_audit_<date>.md`). For every card the skeptic/lane left
OPEN / REFRAME / REJECTED / CONTRADICTION, every mis-formed card, and every lane that wrote 0 surviving cells (SILENT
lane), the FAILURE is classified and the lane's axis is looked up in the audit's axis table; the governing Am Jur sentence
is re-verified against the corpus via turbovec {op:chunk} (quoted only when the chunk really carries the words).
Output: <out>/soul_repair_<lane>_<date>.md — failing cards verbatim, failure class, the Am Jur anchor, a unified diff of the
lane's spec SKILL.md, one acceptance-test line. Idempotent (fingerprint); refuses to write under ~/.hermes/profiles.

usage: soul_repair.py --log <relay.log> --summary <json|auto> --audit <report.md> --out hermes_agent/proposals/ [--no-verify]
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, datetime, difflib, glob, hashlib, json, os, re, sys

ROOT = os.environ.get("BEEZLE_ROOT", str(_eroot()))
PROFILES = os.environ.get("ROOM_PROFILES_DIR", str(_ehermes() / "profiles"))      # READ ONLY here
CARDS_DIR = os.environ.get("ROOM_CARDS_DIR", f"{ROOT}/output/research/rooms_ab")
TODAY = datetime.date.today().isoformat()

META_AXES = {"skeptic"}
PROOF_AXES = {"proof"}
# lane → card form the lane must use (mirror of cards_to_cells.LANE_FORM; names only, no reasoning)
LANE_FORM = {"posture-vehicle": "posture", "dismissal-posture": "posture?", "jurisdiction": "posture?", "remedy": "remedy",
             "vacatur-sequence": "remedy", "vehicle-foreclosure": "posture",
             # F2.1 admin / regulator room lanes — their own forms (cards_to_cells.ADMIN_LANES)
             "actor-map": "actor", "records-request": "request", "formal-demand": "demand",
             "deadline-tracker": "clock", "failure-escalation": "escalation"}
STATUS_RX = re.compile(r"(SUSTAINED|OPEN|REJECTED|REFRAME|NOT IN CORPUS)\b")
CORPUS_TAG_RX = re.compile(r"\[corpus:\s*([A-Za-z0-9_#.-]+)\]")
QUOTE_RX = re.compile(r"[“\"]([^”\"]{4,500})[”\"]")
POINT_RX = re.compile(r"\s*(?:Issue|Point|Ground|Relief)\s+(\d{1,2})\b")
RECORD_DOC_RX = re.compile(r"\b(ORDER_\d+|Control No\.|the order|the complaint|sur-reply|exhibit|docket|praecipe)\b", re.I)

# lane → axes in the audit's axis table (first = default); keyword scoring picks among them per card
LANE_AXES = {
    "void-order": [12, 13], "jurisdiction": [10, 8, 14], "stare-decisis": [20, 1], "duty": [22, 14, 24],
    "posture-vehicle": [11, 10, 13], "procedural": [6, 3, 16], "authority-stack": [20, 9, 1], "dismissal-posture": [4, 3, 1],
    "remedy": [17, 13, 4], "verbatim-rule": [16, 1], "definitions": [26], "construction": [16], "pseudolaw-rebut": [15],
    "order-silence": [8, 9, 18], "appeal-preservation": [5, 6, 7], "vacatur-sequence": [4, 13, 17],
    "vehicle-foreclosure": [11, 25], "demurrer-test": [1, 2, 19, 27], "proof": [9, 8],
}
# lane → words its axis MUST touch / words that mark an OFF-axis answer (wrong_axis class)
LANE_AXIS_WORDS = {
    "void-order": (["void", "voidable", "jurisdiction", "vacate", "set aside", "face of the record", "inspection of the record"], ["immun"]),
    "jurisdiction": (["jurisdiction", "final", "interlocutory", "appealable", "341", "power", "authority to act"], []),
    "stare-decisis": (["bind", "precedent", "stare decisis", "supreme court", "controlling", "higher", "rank"], []),
    "duty": (["shall", "duty", "must", "right to be heard", "accord", "decide", "hear"], []),
    "posture-vehicle": (["vehicle", "motion", "appeal", "5505", "341", "1311", "reconsideration", "toll", "clock", "within 30"], []),
    "remedy": (["relief", "vacate", "open", "strike", "leave to amend", "plead over", "remand"], []),
    "procedural": (["service", "verification", "notice", "deadline", "within", "page", "standard of review", "plead over", "1028"], []),
    "authority-stack": (["bind", "hierarchy", "controlling", "supreme court", "constitution", "rule 2.2", "uphold and apply"], []),
    "dismissal-posture": (["restor", "amend", "plead over", "demurrer", "dismiss", "revive", "open", "vacat"], []),
    "verbatim-rule": (["shall", "may", "must", "text", "rule", "construction"], []),
}
CLASS_ORDER = ["silent_lane", "wrong_axis", "wrong_form", "no_authority_quoted", "anchor_missing", "not_in_corpus_record_doc",
               "not_in_corpus_claimed", "trigger_not_in_record", "overclaim", "rejected_by_skeptic", "verdict_numbering_mismatch", "contradiction_open", "open_unexplained"]


# ------------------------------------------------------------------ ledger
def parse_log(path):
    """relay log → {tag, rooms:[{room, lanes}], ledger}. Chain lines: '<tag>-r1 chain [...]'; LEDGER json is authoritative when present."""
    txt = open(path, encoding="utf-8", errors="ignore").read()
    out = {"tag": None, "rooms": [], "ledger": None, "log": path}
    lm = re.search(r"^LEDGER (\{.*\})\s*$", txt, re.M)
    if lm:
        try:
            out["ledger"] = json.loads(lm.group(1)); out["tag"] = out["ledger"].get("tag")
            out["rooms"] = [{"room": r["room"], "lanes": list(r["lanes"]), "file": r.get("file")} for r in out["ledger"]["rooms"]]
        except Exception:
            out["ledger"] = None
    if not out["rooms"]:
        for i, m in enumerate(re.finditer(r"^(\S+?)-r(\d+) chain \[(.*?)\]", txt, re.M), 1):
            lanes = [x.strip(" '\"") for x in m.group(3).split(",")]
            out["rooms"].append({"room": int(m.group(2)), "lanes": [l for l in lanes if l not in ("skeptic", "proof")], "file": None})
            out["tag"] = out["tag"] or re.sub(r"^beezle-relay-", "", m.group(1))
    return out


def parse_cards(tag, cards_dir, files=None):
    """CARD / VERDICT / RECORD lines → cards, verdicts{(room,axis,n)}, records. Same split rules as cards_to_cells.parse_cards."""
    cards, verdicts, records = [], {}, []
    files = files or sorted(glob.glob(f"{cards_dir}/{tag}_relay_r*_cards.md"))
    for f in files:
        room = int(re.search(r"_r(\d+)_", os.path.basename(f)).group(1)); per_axis = {}; ordinal = 0
        for l in open(f, errors="ignore").read().splitlines():
            if l.startswith("CARD:"):
                p = [x.strip() for x in l[5:].split(" | ")]
                si = next((i for i in range(len(p) - 1, 1, -1) if STATUS_RX.match(p[i].upper())), None)
                if si is None: continue
                n = per_axis[p[0]] = per_axis.get(p[0], 0) + 1
                if p[0] not in META_AXES | PROOF_AXES: ordinal += 1
                tail = p[si + 1:]
                prop = next((x[len("PROPOSITION:"):].strip() for x in tail if x.upper().startswith("PROPOSITION:")), "")
                gate = " | ".join(x for x in tail if x.startswith("GATE:"))
                auth = " ; ".join(p[2:si])
                pm = POINT_RX.match(prop)
                cards.append({"room": room, "n": n, "ordinal": ordinal if p[0] not in META_AXES | PROOF_AXES else None, "axis": p[0],
                              "finding": p[1], "authority": auth, "status": p[si].split()[0].upper(), "proposition": prop, "gate": gate,
                              "point": int(pm.group(1)) if pm else None, "line": l, "file": os.path.basename(f)})
            elif l.startswith("VERDICT:"):
                p = [x.strip() for x in l[8:].split(" | ")]
                if len(p) >= 3:
                    m = re.search(r"\d+", p[1])
                    verdicts[(room, p[0], int(m.group(0)) if m else -1)] = (p[2].split()[0].upper(), p[3] if len(p) > 3 else "", l)
            elif l.startswith("RECORD:"):
                p = [x.strip() for x in l[7:].split(" | ")]
                if len(p) >= 2: records.append({"fact": p[0], "status": p[1].split()[0].upper(), "line": l})
    return cards, verdicts, records


def resolve_verdicts(cards, verdicts):
    """cards_to_cells looks a verdict up by (room, axis, per-axis n) ONLY. The skeptic often numbers cards 1..N across the whole
    room, so that lookup misses and the lane's own status decides. We keep BOTH: `c2c_verdict` (what cards_to_cells saw) and
    `skeptic_verdict` (per-axis n, else the room-global ordinal) — a difference is the class verdict_numbering_mismatch."""
    for c in cards:
        v = verdicts.get((c["room"], c["axis"], c["n"]))
        c["c2c_verdict"] = v[0] if v else None
        c["c2c_effective"] = c["c2c_verdict"] or c["status"]
        if v is None and c["ordinal"] is not None:
            v = verdicts.get((c["room"], c["axis"], c["ordinal"]))
        c["skeptic_verdict"], c["skeptic_reason"], c["verdict_line"] = (v[0], v[1], v[2]) if v else (None, "", "")
        c["effective"] = c["skeptic_verdict"] or c["status"]
    return cards


def summary_auto(cards):
    """the cards_to_cells summary fields this tool needs, re-derived read-only (no cells written, no NEW_UNITS.md touched)."""
    open_by_lane, surviving = [], {}
    for c in cards:
        if c["axis"] in META_AXES | PROOF_AXES: continue
        ok = c["c2c_verdict"] == "SUSTAINED" or (c["c2c_verdict"] is None and c["status"] == "SUSTAINED"
                                                   and "GATE: quote NOT" not in c["gate"] and "carries no quote" not in c["gate"])
        if ok and c["proposition"]:
            surviving[c["axis"]] = surviving.get(c["axis"], 0) + 1
        elif not ok:
            open_by_lane.append(f"{c['axis']}#{c['point']}:{c['c2c_effective']}")
    return {"open_by_lane": open_by_lane, "surviving_by_lane": surviving, "source": "auto"}


# ------------------------------------------------------------------ audit
def parse_audit(path):
    """§2 axis table → {num: {axis, anchors:[{label, quote, chunk}], verdict, raw}}; §3 SPECs → [{name, axes, question}]."""
    txt = open(path, encoding="utf-8", errors="ignore").read()
    axes, specs = {}, []
    for l in txt.splitlines():
        m = re.match(r"^\|\s*(\d{1,2})\s*\|(.*)\|\s*$", l)
        if not m: continue
        cols = [c.strip() for c in m.group(2).split(" | ")]
        if len(cols) < 5: cols = [c.strip() for c in m.group(2).split("|")]
        if len(cols) < 5: continue
        num = int(m.group(1)); axis, anchor_cell, verdict = cols[0], cols[1], cols[-1]
        axes[num] = {"num": num, "axis": re.sub(r"\*\*", "", axis), "anchors": _anchors(anchor_cell), "verdict": re.sub(r"\*\*", "", verdict), "raw": anchor_cell}
    for m in re.finditer(r"^### SPEC ([A-Z]) — `analysis/(\w+)` \(ax(?:es|is) ([^)]*)\)(.*?)(?=^### SPEC|\Z)", txt, re.M | re.S):
        q = re.search(r"\*\*SOUL question[^:]*:\*\*\s*(.+)", m.group(4))
        specs.append({"spec": m.group(1), "name": m.group(2), "axes": [int(x) for x in re.findall(r"\d+", m.group(3))],
                      "question": q.group(1).strip() if q else ""})
    return axes, specs


def _split_outside_quotes(cell):
    """split an axis-table cell at '; ' only when outside “…” / "…" (quoted sentences themselves carry semicolons)."""
    segs, cur, inq = [], "", False
    i = 0
    while i < len(cell):
        ch = cell[i]
        if ch in "“\"" and not inq: inq = True
        elif ch in "”\"" and inq: inq = False
        if ch == ";" and not inq and cell[i + 1:i + 2] == " ":
            segs.append(cur); cur = ""; i += 2; continue
        cur += ch; i += 1
    segs.append(cur)
    return [s for s in segs if s.strip()]


def _anchors(cell):
    """'**§ 298. Heading** "sentence" [corpus:id]; ...' → anchors; a quote pairs with the FIRST [corpus:id] after it; a heading
    without a quote keeps the heading as label (quote=None → never quoted, only cited)."""
    out = []
    for seg in _split_outside_quotes(cell):
        ids = [(m.start(), m.group(1)) for m in CORPUS_TAG_RX.finditer(seg)]
        if not ids: continue
        label = re.search(r"\*\*([^*]+)\*\*", seg)
        quotes = [(m.start(), m.group(1).strip()) for m in QUOTE_RX.finditer(seg) if len(m.group(1).strip()) >= 25]
        plain = CORPUS_TAG_RX.sub("", re.sub(r"\*\*", "", seg)).strip(" ;.")
        if quotes:
            for qpos, q in quotes:
                cid = next((c for p, c in ids if p > qpos), ids[-1][1])
                lab = label.group(1).strip() if label else plain[:qpos].split(" (")[0].strip(" :;—-\"“") or plain[:60]
                out.append({"label": lab[:90], "quote": q, "chunk": cid})
        else:
            out.append({"label": (label.group(1).strip() if label else plain[:90]), "quote": None, "chunk": ids[0][1]})
    return out


# ------------------------------------------------------------------ corpus verification
def _norm(s):
    s = s.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def default_fetch(chunk_id):
    """turbovec {op:chunk}. Returns text or None. Import kept lazy so fixtures never touch the daemon."""
    sys.path.insert(0, ROOT)
    try:
        from pipeline import retrieval_client as rc
        r = rc.chunk(chunk_id)
        return r.get("text") if isinstance(r, dict) and not r.get("down") else None
    except Exception:
        return None


def verify_anchor(anchor, fetch=default_fetch, cache=None):
    """every quote segment (split at … / ...) of ≥ 12 chars must be a substring of the normalized chunk text."""
    cid, q = anchor["chunk"], anchor.get("quote")
    cache = cache if cache is not None else {}
    if cid not in cache: cache[cid] = fetch(cid)
    txt = cache[cid]
    if txt is None: return {**anchor, "verified": False, "why": "chunk not returned by {op:chunk}"}
    if not q: return {**anchor, "verified": True, "why": "heading-only anchor (cited, not quoted)", "cite_only": True}
    segs = [_norm(s) for s in re.split(r"…|\.\.\.", q)]
    segs = [s for s in segs if len(s) >= 12] or [_norm(q)]
    nt = _norm(txt)
    if all(s in nt for s in segs):
        return {**anchor, "verified": True, "why": "chunk carries every quoted segment (exact)"}
    # Am Jur print-outs are two-column OCR: the other column's words interleave the sentence. Accept an ORDERED subsequence of
    # ≥ 85% of the segment's tokens inside a window ≤ 4× the segment length — the words are there, in order, on that chunk.
    if all(_subseq_ratio(s.split(), nt.split()) >= 0.85 for s in segs):
        return {**anchor, "verified": True, "why": "chunk carries the quoted words in order (OCR two-column interleave)", "ocr": True}
    # short tail segments ('… must be wholly insufficient') lose a word to a column-break hyphen ('wholly in|sufficient'):
    # accept when every token of the segment but one is in the window AND the missing token's first 2 letters open the next word
    if all(_subseq_ratio(s.split(), nt.split()) >= 0.85 or _hyphen_break(s.split(), nt) for s in segs):
        return {**anchor, "verified": True, "why": "chunk carries the quoted words (OCR column-break hyphen)", "ocr": True}
    return {**anchor, "verified": False, "why": "quoted words NOT in chunk"}


def _hyphen_break(q, nt):
    """q minus one token is an ordered subsequence (≥ 0.85) and the missing token's 2-letter head + its tail both occur in nt."""
    toks = nt.split()
    for i, w in enumerate(q):
        rest = q[:i] + q[i + 1:]
        if len(rest) >= 3 and _subseq_ratio(rest, toks) >= 0.85 and len(w) >= 6 and (f" {w[:2]}" in " " + nt and w[2:] in nt):
            return True
    return False


def _subseq_ratio(q, t):
    """best fraction of q's tokens matched in order within any window of t no longer than 4*len(q)+8."""
    best = 0.0
    starts = [i for i, w in enumerate(t) if w == q[0]] or [0]
    for st in starts:
        j, hit = st, 0
        lim = min(len(t), st + 4 * len(q) + 8)
        for w in q:
            k = next((x for x in range(j, min(lim, j + 14)) if t[x] == w), None)   # ≤ 13 interleaved/garbled tokens between hits
            if k is not None: hit += 1; j = k + 1
        best = max(best, hit / len(q))
    return best


# ------------------------------------------------------------------ classification
def _form_of(prop):
    if re.match(r"\s*Vehicle:", prop or ""): return "posture"
    if re.match(r"\s*Relief\s+\d", prop or ""): return "remedy"
    if re.search(r"\bRule:", prop or "") and re.search(r"\bViolation:", prop or ""): return "violation"
    return "none"


def _violation_text(prop):
    m = re.search(r"Violation:\s*(.*)$", prop or "", re.S)
    return m.group(1).strip() if m else ""


def classify_card(c):
    """→ list of failure classes (CLASS_ORDER order) with one-line reasons; [] when the card is clean."""
    out = []
    text = f"{c['finding']} {c['authority']} {c['proposition']}"
    low = text.lower()
    lane = c["axis"]
    eff = c["effective"]; c2c = c["c2c_effective"]
    rule_quote = bool(QUOTE_RX.search(re.sub(r"Violation:.*$", "", c["proposition"] or "", flags=re.S)))
    # wrong axis: the lane's own words absent, an off-axis marker present
    must, off = LANE_AXIS_WORDS.get(lane, ([], []))
    if must and not any(w in low for w in must):
        out.append(("wrong_axis", f"none of the lane's axis words {must[:4]}… appear; the card answers another axis"))
    elif off and any(w in low for w in off):
        out.append(("wrong_axis", f"off-axis marker {[w for w in off if w in low]} — the quoted authority answers a different question (immunity ≠ void/voidable)"))
    # wrong form
    want = LANE_FORM.get(lane, "violation"); have = _form_of(c["proposition"]); viol = _violation_text(c["proposition"])
    if want == "posture" and have == "violation" and viol and not re.match(r"None\b", viol):
        out.append(("wrong_form", "posture lane wrote a violation-form card with a real Violation: — a VIOLATION misfiled on the posture axis (belongs to the lane that owns that rule)"))
    elif want == "remedy" and have == "violation":
        out.append(("wrong_form", "remedy lane wrote the violation form; the contract is 'Relief n: <ask> — Rule: “…” <cite> [corpus:<chunk>]. Because: <record fact>'"))
    elif want == "violation" and have == "violation" and re.match(r"None\b", viol):
        out.append(("wrong_form", "violation-form card with 'Violation: None' — posture/classification content in the violation form; no record fact → no cell"))
    # authority
    if "carries no quote" in c["gate"] or (not rule_quote and c["authority"] and "NOT IN CORPUS" not in c["authority"]):
        out.append(("no_authority_quoted", "GATE: authority carries no quote — rule paraphrased, no verbatim words, nothing for the verifier to check"))
    if "GATE: quote NOT" in c["gate"] or "did not carry" in c["gate"]:
        out.append(("anchor_missing", "the card's own [corpus:<chunk>] does not carry the quoted words"))
    reason = (c.get("skeptic_reason") or "") + " " + c["status"]
    if "NOT IN CORPUS" in reason.upper() or "NOT IN CORPUS" in c["authority"].upper():
        if RECORD_DOC_RX.search(c["authority"]) and not re.search(r"\d+ [A-Z]\.\d*d|v\. ", c["authority"]):
            out.append(("not_in_corpus_record_doc", "NOT IN CORPUS claimed for a RECORD document — record docs are cited record:<heading>, never searched in the law corpus"))
        else:
            out.append(("not_in_corpus_claimed", f"skeptic: {c.get('skeptic_reason') or c['status']} — the case is not in the pack; the Am Jur statement of the same rule IS in corpus (anchor below)"))
    if re.search(r"assumes|prerequisite|not established in (the )?record|not in the record|fact not in", reason, re.I):
        out.append(("trigger_not_in_record", f"skeptic: {c.get('skeptic_reason')} — the rule's trigger (the predicate fact) is not on the record slice; the rule is right, the record fact is missing or the rule needs a trigger-less sibling"))
    if eff == "REFRAME" or (eff == "REJECTED" and re.search(r"overstat|claims more|not in cited|doesn't require|does not require", reason, re.I)):
        out.append(("overclaim", f"skeptic: {c.get('skeptic_reason')} — the proposition claims more than the quoted authority holds"))
    elif eff == "REJECTED":
        out.append(("rejected_by_skeptic", f"skeptic: {c.get('skeptic_reason')}"))
    if c["skeptic_verdict"] and c["c2c_verdict"] is None and c["skeptic_verdict"] != c["status"]:
        out.append(("verdict_numbering_mismatch", f"skeptic numbered the card {c['ordinal']} (room-global) but cards_to_cells keys per-axis #{c['n']}; skeptic said {c['skeptic_verdict']}, the lane's own status {c['status']} decided"))
    if lane in PROOF_AXES and c["finding"].startswith("CONTRADICTION") and eff != "SUSTAINED":
        out.append(("contradiction_open", "proof CONTRADICTION left OPEN — a lane must convert it into a graded card with the rule it offends"))
    if c2c not in ("SUSTAINED",) and not out and lane not in META_AXES:
        out.append(("open_unexplained", f"set aside as 'verdict {c2c}' with no classifiable cause in the card text"))
    out.sort(key=lambda t: CLASS_ORDER.index(t[0]))
    return out


def pick_axis(lane, card_text, axes):
    """among the lane's axes, the one whose axis sentence shares the most ≥5-letter words with the card; ties → lane default order."""
    cands = LANE_AXES.get(lane, [])
    words = set(w for w in re.findall(r"[a-z]{5,}", card_text.lower()))
    best, score = None, -1
    for i, n in enumerate(cands):
        if n not in axes: continue
        aw = set(re.findall(r"[a-z]{5,}", axes[n]["axis"].lower()))
        s = len(words & aw) + (2 if i == 0 else 0)      # the lane's own axis wins unless another is clearly the card's subject
        if s > score: best, score = n, s
    return best


# ------------------------------------------------------------------ proposal
def spec_path(lane):
    return f"{PROFILES}/asp-{lane}/skills/legal/{lane}-spec/SKILL.md"


def proposed_spec(lane, current, axis_rows, date):
    """insert an '## AM JUR ANCHOR' block after '## THE QUESTION' (or at the end) — the question + verified sentence + form rule."""
    lines = []
    for ax, anchors, failures in axis_rows:
        lines.append(f"- Axis {ax['num']} — {ax['axis'].split(' — ')[0][:90]}: ASK IT on every order of this class.")
        for a in anchors:
            if a["verified"] and a.get("quote"):
                lines.append(f"  Am Jur: \"{a['quote'][:220]}\" ({a['label'][:60]}) [corpus:{a['chunk']}]")
            elif a["verified"]:
                lines.append(f"  Am Jur: {a['label'][:80]} [corpus:{a['chunk']}] (heading chunk — cite, do not quote)")
    seen = []
    for _, _, failures in axis_rows:
        for cls, _w in failures:
            if cls not in seen: seen.append(cls)
    for cls in seen:
        lines.append(f"- FIX {cls}: {_fix_rule(cls, lane)}")
    block = f"\n## AM JUR ANCHOR — soul repair {date} (proposal; parent applies)\n" + "\n".join(lines) + "\n"
    if "## THE QUESTION" in current:
        head, rest = current.split("## THE QUESTION", 1)
        m = re.search(r"\n## ", rest[1:])
        cut = m.start() + 1 if m else len(rest)
        return head + "## THE QUESTION" + rest[:cut] + block + rest[cut:]
    return current.rstrip("\n") + "\n" + block


def _fix_rule(cls, lane):
    return {
        "silent_lane": "post ≥ 1 card on the point even when the answer is 'erroneous, not void' — classification IS the card; silence is a failed lane",
        "wrong_axis": "quote only authority that answers THIS lane's question; an authority on another axis goes to that lane via NOTE_FOR_SYNTH",
        "wrong_form": f"use the {LANE_FORM.get(lane, 'violation')} form exactly as cards_to_cells parses it; a real Violation belongs to the owning lane",
        "no_authority_quoted": "Rule: must carry the verbatim words in quotes + cite + [corpus:<chunk>]; a paraphrase is not authority",
        "anchor_missing": "name the chunk that carries the words you quote (the pack's chunk_id), or NOT IN CORPUS",
        "not_in_corpus_record_doc": "record documents are record:<heading>, never searched in the law corpus",
        "not_in_corpus_claimed": "when the case is not in the pack, state the SAME rule from the Am Jur section in the pack (anchor above) — the rule survives, the cite changes",
        "trigger_not_in_record": "quote the rule only with its trigger fact cited record:<heading>; when the trigger is absent, use the rule that needs no trigger (e.g. the duty to decide / general-ruling presumption) and say the trigger is NOT IN RECORD",
        "overclaim": "claim exactly what the quoted sentence holds; 'nullity' / 'void' only when the quoted rule says so",
        "rejected_by_skeptic": "answer the skeptic's reason in the next card; never repeat a rejected card verbatim",
        "verdict_numbering_mismatch": "number cards per-axis (<lane>#1, #2 …) so the skeptic's VERDICT keys match cards_to_cells",
        "contradiction_open": "convert the proof CONTRADICTION into 'Issue n — Rule: … Violation: <the contradiction>' on the lane that owns the rule",
        "open_unexplained": "re-read the card against the pack; every field of the form must be filled from the pack or NOT IN CORPUS",
    }.get(cls, "")


def fingerprint(lane, failures):
    h = hashlib.sha1()
    for f in sorted(failures, key=lambda f: f["card"]["line"]):
        h.update(f["card"]["line"].encode()); h.update(",".join(c for c, _ in f["classes"]).encode())
    return h.hexdigest()[:12]


def build_proposals(log, cards, records, summary, axes, specs, out_dir, date=TODAY, fetch=default_fetch, verify=True, cache=None):
    rooms = {r["room"]: r["lanes"] for r in log["rooms"]}
    chain_lanes = [l for r in log["rooms"] for l in r["lanes"]]
    cache = cache if cache is not None else {}
    by_lane = {}
    for c in cards:
        if c["axis"] in META_AXES: continue
        cls = classify_card(c)
        if not cls: continue
        lane = c["axis"]
        if lane in PROOF_AXES:    # route a record contradiction to the lane whose axis it names
            lane = _route_contradiction(c)
        by_lane.setdefault(lane, []).append({"card": c, "classes": cls})
    # silent lanes: in the chain, 0 surviving cells
    for lane in chain_lanes:
        if summary["surviving_by_lane"].get(lane, 0) == 0:
            n_cards = sum(1 for c in cards if c["axis"] == lane)
            by_lane.setdefault(lane, []).insert(0, {"card": {"line": f"(lane {lane}: {n_cards} card(s), 0 surviving cells)", "axis": lane, "point": None,
                                                              "proposition": "", "finding": "", "authority": "", "verdict_line": "", "skeptic_reason": "", "effective": "SILENT"},
                                                     "classes": [("silent_lane", f"lane in ROOM_CHAIN wrote {n_cards} card(s) and 0 cells reached cells.jsonl")]})
    written, skipped, report = [], [], {}
    os.makedirs(out_dir, exist_ok=True)
    _guard_out(out_dir)
    for lane, fails in sorted(by_lane.items()):
        fp = fingerprint(lane, fails)
        existing = [p for p in glob.glob(f"{out_dir}/soul_repair_{lane}_*.md") if f"fingerprint: {fp}" in open(p, encoding="utf-8", errors="ignore").read()]
        if existing:
            skipped.append(existing[0]); report[lane] = {"skipped": existing[0], "fingerprint": fp}; continue
        axis_rows = []
        seen = set()
        for f in fails:
            ctext = f"{f['card'].get('finding', '')} {f['card'].get('proposition', '')} {f['card'].get('authority', '')}"
            n = pick_axis(lane, ctext, axes) or (LANE_AXES.get(lane) or [None])[0]
            if n is None or n in seen or n not in axes: continue
            seen.add(n)
            anchors = [verify_anchor(a, fetch=fetch, cache=cache) if verify else {**a, "verified": False, "why": "verification skipped (--no-verify)"} for a in axes[n]["anchors"]]
            all_cls = []
            for g in fails:
                for cls, why in g["classes"]:
                    if cls not in [x for x, _ in all_cls]: all_cls.append((cls, why))
            axis_rows.append((axes[n], anchors, all_cls))
        # a proposal must carry a QUOTABLE verified sentence: walk the lane's other axes until one does
        def _has_quote(rows): return any(a["verified"] and a.get("quote") for _, an, _ in rows for a in an)
        for n in LANE_AXES.get(lane, []):
            if _has_quote(axis_rows): break
            if n in seen or n not in axes: continue
            seen.add(n)
            anchors = [verify_anchor(a, fetch=fetch, cache=cache) if verify else {**a, "verified": False, "why": "verification skipped (--no-verify)"} for a in axes[n]["anchors"]]
            axis_rows.append((axes[n], anchors, axis_rows[0][2] if axis_rows else [c for f in fails for c in f["classes"]]))
        order = LANE_AXES.get(lane, [])
        axis_rows.sort(key=lambda r: order.index(r[0]["num"]) if r[0]["num"] in order else 99)   # lane's own axis first
        path = f"{out_dir}/soul_repair_{lane}_{date}.md"
        body = render(lane, fails, axis_rows, log, summary, date, fp, specs)
        open(path, "w", encoding="utf-8").write(body)
        written.append(path)
        report[lane] = {"written": path, "fingerprint": fp, "classes": sorted({c for f in fails for c, _ in f["classes"]}),
                        "axes": [ax["num"] for ax, _, _ in axis_rows],
                        "verified_anchors": [a["chunk"] for _, an, _ in axis_rows for a in an if a["verified"]],
                        "unverified_anchors": [a["chunk"] for _, an, _ in axis_rows for a in an if not a["verified"]]}
    return {"written": written, "skipped": skipped, "lanes": report,
            "class_counts": _class_counts(by_lane), "silent_lanes": [l for l, fs in by_lane.items() if any(c == "silent_lane" for f in fs for c, _ in f["classes"])]}


def _route_contradiction(c):
    t = (c["finding"] + " " + c["proposition"]).lower()
    if "response" in t or "heard" in t or "pending" in t: return "duty"
    if "answer" in t and "prejudice" in t: return "order-silence"
    if "documents" in t or "elements" in t or "admits" in t: return "demurrer-test"
    return "order-silence"


def _class_counts(by_lane):
    cnt = {}
    for fs in by_lane.values():
        for f in fs:
            for c, _ in f["classes"]: cnt[c] = cnt.get(c, 0) + 1
    return dict(sorted(cnt.items()))


def _guard_out(out_dir):
    real = os.path.realpath(out_dir)
    if real.startswith(os.path.realpath(PROFILES)) or "/.hermes/profiles" in real:
        raise SystemExit(f"soul_repair: refusing to write proposals under {PROFILES} (parent applies; proposals live in hermes_agent/proposals/)")


def render(lane, fails, axis_rows, log, summary, date, fp, specs):
    L = [f"# SOUL REPAIR PROPOSAL — lane `{lane}` — {date}", "",
         f"source log: {log['log']}  · tag `{log['tag']}` · summary: {summary.get('source')} · fingerprint: {fp}",
         "status: PROPOSAL — NOT APPLIED. The parent applies (SKILL.md edits to an existing asp-* spec go as SKILL.proposed.md + this diff).", "",
         "## 1. Failing cards (verbatim)"]
    for f in fails:
        L.append(f"- `{f['card']['line'][:1200]}`")
        if f["card"].get("verdict_line"): L.append(f"  - skeptic: `{f['card']['verdict_line']}`")
        L.append(f"  - effective verdict: **{f['card'].get('effective')}**")
    L += ["", "## 2. Classified failure"]
    for f in fails:
        for cls, why in f["classes"]:
            L.append(f"- **{cls}** — {why}")
    L += ["", "## 3. Am Jur section that governs the question (verified via turbovec {op:chunk})"]
    if not axis_rows: L.append("- (no axis row in the audit table for this lane)")
    for ax, anchors, _ in axis_rows:
        L.append(f"- **Axis {ax['num']}** — {ax['axis'][:300]}  (audit verdict: {ax['verdict']})")
        for a in anchors:
            if a["verified"] and a.get("quote"):
                L.append(f"  - VERIFIED [corpus:{a['chunk']}] {a['label'][:80]}: \"{a['quote']}\"")
            elif a["verified"]:
                L.append(f"  - VERIFIED (heading chunk, cite only) [corpus:{a['chunk']}] {a['label'][:80]}")
            else:
                L.append(f"  - NOT VERIFIED [corpus:{a['chunk']}] {a['label'][:80]} — {a['why']} — NOT quoted")
    L += ["", "## 4. Proposed SKILL.md diff (unified; apply to the lane's spec skill)"]
    sp = spec_path(lane)
    cur = open(sp, encoding="utf-8", errors="ignore").read() if os.path.exists(sp) else ""
    if cur:
        new = proposed_spec(lane, cur, axis_rows, date)
        diff = difflib.unified_diff(cur.splitlines(True), new.splitlines(True), fromfile=f"a/asp-{lane}/skills/legal/{lane}-spec/SKILL.md",
                                    tofile=f"b/asp-{lane}/skills/legal/{lane}-spec/SKILL.md")
        L += ["```diff", "".join(diff).rstrip("\n"), "```"]
    else:
        spec = next((s for s in specs if lane.replace("-", "_") in s["name"] or s["name"].replace("_", "-") in lane), None)
        L.append(f"- lane `{lane}` has no spec at `{sp}` — NEW LANE; the audit SPEC `{spec['name'] if spec else '?'}` is its seed"
                 + (f": {spec['question'][:300]}" if spec else "") + ". See `_sandbox_soul_repair/lanes/asp-{lane}/` for the ≤2 KB spec.")
    L += ["", "## 5. Acceptance test"]
    for ax, anchors, _ in axis_rows:
        good = next((a for a in anchors if a["verified"] and a.get("quote")), None)
        pts = sorted({f["card"]["point"] for f in fails if f["card"].get("point")}) or ["N"]
        cite = f"{good['label'][:50]} [corpus:{good['chunk']}]" if good else "a verified in-pack chunk"
        L.append(f"- next room run: lane `{lane}` posts ≥ 1 SUSTAINED card on point {'/'.join(map(str, pts))} quoting {cite}, in the {LANE_FORM.get(lane, 'violation').rstrip('?')} form, and it reaches cells.jsonl (0 → ≥ 1 row).")
    L += ["", "## 6. Apply (parent only — NOT run by this tool)",
          f"1. `cp {sp} {sp}.bak.$(date +%Y%m%d_%H%M%S)`", f"2. write the block above into `{os.path.dirname(sp)}/SKILL.proposed.md`; review; `cp SKILL.proposed.md SKILL.md`",
          "3. re-run the relay with the same inputs; run `cards_to_cells`; check the acceptance line.", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", required=True); ap.add_argument("--summary", default="auto"); ap.add_argument("--audit", required=True)
    ap.add_argument("--out", default=f"{ROOT}/hermes_agent/proposals"); ap.add_argument("--cards-dir", default=CARDS_DIR)
    ap.add_argument("--date", default=TODAY); ap.add_argument("--no-verify", action="store_true"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    log = parse_log(a.log)
    files = [r["file"] for r in log["rooms"] if r.get("file") and os.path.exists(r["file"])] or None
    cards, verdicts, records = parse_cards(log["tag"], a.cards_dir, files)
    resolve_verdicts(cards, verdicts)
    summary = summary_auto(cards)
    if a.summary != "auto":
        given = json.load(open(a.summary)); summary["given_open_by_lane"] = given.get("open_by_lane"); summary["source"] = a.summary
    axes, specs = parse_audit(a.audit)
    rep = build_proposals(log, cards, records, summary, axes, specs, a.out, date=a.date, verify=not a.no_verify)
    rep["summary"] = summary; rep["cards"] = len(cards); rep["axes_in_audit"] = len(axes)
    print(json.dumps(rep, indent=1 if not a.json else None, ensure_ascii=False))
    return rep


if __name__ == "__main__":
    main()
