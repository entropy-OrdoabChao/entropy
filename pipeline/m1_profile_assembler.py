"""m1_profile_assembler — the PROFILE-DRIVEN lane (S-filing-type-on-demand, 2026-11-14).

No per-instrument spec YAML. The anatomy of a filing is DERIVED at run time from three things that already exist:
  1. the room's SUSTAINED instrument card  (INSTRUMENT: <name> | CLASS: <class> | ANATOMY: … | RULE THAT MAKES IT THIS CLASS: "…" <cite>)
  2. the court profile  output/audit/m1_style_profiles.json <court>.<kind>  (mandatory_sections / mandatory_attachments, derived
     from the corpus by pipeline/rule_requirements.py) — and when the kind is missing it is derived ON DEMAND from the rule text
     the instrument card names (rule_requirements.derive_requirements + upsert_profile), never from a table here
  3. the VERBATIM rule text (m1_spec_assembler.rule_texts — Pa.R.C.P. / Pa.R.A.P. / Phila.Civ.R. by SOURCE KEY) + the gated cells.

Comprehension (which cells make which objection, what each paragraph says) is the LLM's. Every guardrail is deterministic and
reads the record: a cited "Compl. ¶n" must EXIST in the adverse paper; a quoted rule must be VERBATIM in the rule text / cells
(quote_fidelity); every ground cited must be WORDED in the governing rule's own text; banned labels never pass; no attorney
reference in a proposed order. A section that fails its guardrail twice is a FAIL, never a fallback draft.
"""
import json, os, re, sys
from pathlib import Path

from pipeline import m1_spec_assembler as S

PROFILES = Path("output/audit/m1_style_profiles.json")
BANNED = re.compile(r"\b(pseudo-?law|frivolous|sovereign citizen|pro se|fraud on the court|corrupt)\b", re.I)
ADV_CITE = re.compile(r"(?:Compl\.|Complaint)\s*¶\s*(\d+)|Compl\.\s*Ex\.\s*([A-Z0-9-]+)", re.I)
ATTY_REF = re.compile(r"\b(Esq\.?|Esquire|Attorney I\.?D|counsel for|Bar No\.?|Attorney for)\b", re.I)


# ------------------------------------------------------------------ profile kind: look up, else derive on demand
def _profiles():
    try:
        return json.loads(PROFILES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def kind_for(court, ftype, side):
    """the profile kind that governs this filing: <side>_<ftype> → <ftype> → None (caller derives)."""
    prof = _profiles().get(court, {})
    for k in (f"{side}_{ftype}" if side else "", ftype):
        if k and isinstance(prof.get(k), dict) and prof[k].get("mandatory_sections"):
            return k
    return None


def derive_kind_on_demand(court, ftype, side, rule_cite, rule_text, llm_url):
    """No profile kind for this filing → derive it from the governing rule's VERBATIM text (rule_requirements: LLM derives,
    the rule text vetoes every heading) and UPSERT it into the profile. Returns the kind or None (nothing anchored)."""
    from pipeline import rule_requirements as RR
    kind = f"{side}_{ftype}" if side else ftype
    if not rule_text:
        return None
    reqs = RR.derive_requirements(rule_text, court, kind, filing_desc=ftype.replace("_", " "), rule_id=rule_cite)
    if not (reqs or {}).get("sections"):
        return None
    RR.upsert_profile(court, kind, reqs, rule_cite, [rule_cite], apply=True)
    return kind


# ------------------------------------------------------------------ rule text pool (verbatim; by source key)
def rule_pool(cites):
    """{cite: text} for every Pa.R.C.P./Pa.R.A.P./Phila.Civ.R. cite; a cite with no text is reported, never quoted."""
    out, missing = {}, []
    got = S.rule_texts([c for c in dict.fromkeys(cites) if c])
    for c, v in got.items():
        if v and v.get("text"):
            out[c] = v["text"]
        else:
            missing.append(c)
    return out, missing


def grounds_in_rule(rule_text):
    """the enumerated grounds AS WORDED in the rule ('(1) lack of jurisdiction …'), deterministic from the text —
    the LLM may cite only a ground that appears here."""
    out = {}
    for m in re.finditer(r"\((\d{1,2})\)\s+([^;\n]{8,220})", rule_text or ""):
        n = m.group(1)
        if n not in out:
            out[n] = re.sub(r"\s+", " ", m.group(2)).strip(" ;.")
    return out


# ------------------------------------------------------------------ LLM steps (bounded; one retry each)
SYS_PLAN = ("You plan a Pennsylvania PLEADING for a party appearing sui juris. You receive: the governing rule's enumerated grounds "
            "(verbatim), the adverse paper's numbered paragraphs, and graded CELLS (room findings). Group the cells into the objections "
            "the rule allows: each objection = ONE enumerated ground, a short heading, the adverse paragraphs it attacks, the cell indexes "
            "it rests on. Use ONLY grounds listed; never invent a ground; never merge two grounds in one objection; at most {maxn} objections; "
            "a ground must FIT the action as pleaded (an action AT LAW — e.g. mortgage foreclosure — admits no 'adequate non-statutory remedy at law' "
            "objection, that ground attacks a bill in equity); a cell that attacks the pleader's VERIFICATION, its CAPACITY as servicer, or an "
            "unpleaded CONDITION PRECEDENT belongs under the conform-to-law / specificity grounds and must not be dropped; "
            "a ground with no supporting cell is omitted. Output STRICT JSON list: "
            "[{{\"ground\": \"<number as listed>\", \"heading\": \"<≤ 14 words>\", \"adverse_paras\": [n, …], \"cells\": [i, …]}}]. No prose.")

SYS_PARAS = ("You write the NUMBERED PARAGRAPHS of one preliminary objection in a Pennsylvania pleading for a party sui juris. The RULE TEXT "
             "given to you governs the form (what a pleading paragraph may contain, how a ground must be stated) — obey it as written. "
             "Rules: (1) every paragraph states ONE fact or ONE rule; a FACT paragraph CITES (no quotation marks needed); a RULE paragraph QUOTES; "
             "(2) a fact paragraph cites the adverse paper paragraph it rests on as "
             "'Compl. ¶n' (only paragraph numbers given to you) or names the exhibit 'Compl. Ex. X' or the record fact from a RECORD FACT cell; "
             "(3) a rule paragraph quotes the rule VERBATIM in quotation marks (copy ≥ 6 consecutive words exactly from RULE TEXT or a CELL) with its cite; "
             "(4) the first paragraph incorporates the foregoing paragraphs by reference; the last paragraph states the ground in the rule's own words "
             "and the consequence the rule provides; (5) no adjectives, no argument, no motive words, never 'pro se'; the objecting party is 'Defendant' and signs sui juris; "
             "(6) NEVER adopt the adversary's averment as Defendant's own fact — write 'Compl. ¶n pleads …', not 'Defendant's address is …'; Defendant's facts "
             "come from the CELLS (e.g. the address of record, non-receipt of notice) and are stated as Defendant's; (7) write NO tags or labels "
             "('Record Fact', 'record:', '[corpus:…]') and no sentence about these drafting rules — the court reads every word. "
             "Use ONLY the CELLS and RULE TEXT given — if no cell supports a fact, do not assert it. Output STRICT JSON list of strings (one per paragraph, "
             "{lo}–{hi} paragraphs). No prose outside JSON.")

SYS_WHEREFORE = ("Write the WHEREFORE clause of one preliminary objection in a Pennsylvania pleading: one sentence, the relief the rule allows for "
                 "that ground (dismiss / strike / require a more specific pleading / sustain), nothing else, no adjectives. Output a JSON string.")

SYS_SECTION = ("You write ONE section of a Pennsylvania court filing for a party sui juris. The section's HEADING and what the court rule REQUIRES "
               "of it are given; so are the pleading body already written, the graded CELLS and the VERBATIM RULE TEXT. Write only what the rule "
               "requires for this heading. Quote rules VERBATIM in quotation marks (≥ 6 consecutive words copied exactly) with their cite; cite the "
               "adverse paper as 'Compl. ¶n' using only numbers that appear in the body; never 'pro se', never motive words. Use ONLY the material "
               "given. Output a JSON string (markdown allowed inside; sub-headings as '### Title'). No prose outside JSON.")

# S-brief-argues-the-cells (po-16): the brief is the ONE place the room's law reaches the court. Without this contract the model wrote
# the brief from the pleading body (facts + the 1028 ground text) and dropped every authority the cells carried — four runs, zero law
# for the dispositive objection. Class rule for every BRIEF / MEMORANDUM section, any forum, any filing type.
SYS_BRIEF_CONTRACT = ("\n\nBRIEF CONTRACT (mandatory for a BRIEF / MEMORANDUM OF LAW heading): write '### ARGUMENT' with ONE numbered sub-section per "
                      "objection / count in the PLEADING BODY, same order, titled '#### <Roman>. <the objection's heading>'. Each sub-section states, in this "
                      "order: (1) the ground — the rule quoted VERBATIM with cite; (2) the LAW — every authority the CELLS for that objection's units carry "
                      "(statute / rule / definition / treatise / case), quoted verbatim where the cell quotes it, each with its cite exactly as the cell gives it; "
                      "an authority in the cells for that objection that you omit must be named with the reason; (3) the RECORD — the pleading ¶¶ and exhibits "
                      "(by letter) that satisfy each element; (4) the consequence the rule provides. Argue from the cells' LAW: and AUTH: fields — a sub-section "
                      "that cites no authority from the cells is incomplete. Length is not a constraint; completeness is.")

SYS_NTP = ("Read the FORUM RULE TEXT. Answer which of the listed enumerated grounds REQUIRE a notice to plead / an answer when raised by "
           "preliminary objection, exactly as the rule text says. Output STRICT JSON: {\"grounds\": [\"<number>\", …], \"quote\": \"<the verbatim words>\"}.")


class LLMTransportError(RuntimeError):
    """the model endpoint did not answer — a plumbing fault, never reported as a bad model answer"""


def _llm(url, system, user, **kw):
    try:
        return S.llm_json(url, system, user, **kw)
    except (OSError, ConnectionError) as e:          # URLError/ConnectionRefused: the endpoint is down — stop the run, say so
        raise LLMTransportError(f"LLM endpoint {url} unreachable: {e}") from e
    except Exception as e:
        return {"__error__": f"{e.__class__.__name__}: {e}"}


def _adv_cites_ok(text, adv_paras):
    bad = []
    for m in ADV_CITE.finditer(text):
        if m.group(1) and int(m.group(1)) not in adv_paras:
            bad.append(f"Compl. ¶{m.group(1)} does not exist in the adverse paper")
    return bad


QUOTE_SPAN = re.compile(r'(?:(?<=^)|(?<=[\s(\[—–-]))["“]((?:(?!["“”])[^\n]){12,}?)["”](?=[\s.,;:)\]]|$)', re.M)   # directional + single-line: a closing ” never pairs with the next opening “ (2026-11-14: the brief section "failed" on the prose BETWEEN two real quotes)
RULE_CITE = re.compile(r"(Pa\.R\.C\.P\.|Pa\.R\.A\.P\.|Phila\.\s*Civ\.\s*R\.|Pa\.C\.S\.|U\.S\.C\.|C\.F\.R\.)")


def _n(x):
    """compare words, not punctuation: the quoter's periods/commas/semicolons are not a difference in the law"""
    return re.sub(r"[^\w§¶$./-]+", " ", S._norm(x)).strip()


def _pieces(span):
    """a quotation may skip text with an ellipsis — each piece is verified on its own (verbatim means every piece verbatim);
    terminal punctuation inside the quotation marks (. , ; :) belongs to the quoter's sentence and is not compared"""
    return [pc.strip().rstrip(".,;:") for pc in re.split(r"\s*(?:…|\.\s?\.\s?\.)\s*", span) if pc.strip()]


def _quotes_verbatim(spans, corpora):
    """every ≥6-word quoted span (or ellipsis piece) must sit verbatim in the corpora; the message carries the FULL offending words
    and the closest passage so the retry can see where it diverged (a message cut at 80 chars taught the model nothing)."""
    hay = _n("\n".join(corpora)); out = []
    for sp in spans:
        for pc in _pieces(sp):
            if len(pc.split()) < 6 or _n(pc) in hay:
                continue
            head = " ".join(pc.split()[:5]); j = hay.find(_n(head))
            near = ("the source reads: “" + hay[j:j + len(pc) + 80] + "”") if j >= 0 else "those opening words appear nowhere in the rule text, cells or adverse paper"
            out.append(f"quote not verbatim — you wrote “{pc}”; {near}. Copy the source exactly or drop the quotation marks and cite instead.")
    return out


def _states(paragraph, grounds_text, n=5):
    """does the paragraph carry ≥ n consecutive words of one enumerated ground, as the rule words it"""
    hp = _n(paragraph)
    for g in grounds_text:
        w = _n(g).split()
        if len(w) <= n and _n(g) in hp:
            return True
        if any(" ".join(w[i:i + n]) in hp for i in range(0, max(1, len(w) - n + 1))):
            return True
    return False


_FIG = re.compile(r"\$?\d[\d,]*\.\d\d|\b\d{4,}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b[A-Z]{2,5}-?\d{2,}\b|\b1099-?[A-Z]\b")


def _in_record(paragraph, record_texts):
    """is this fact IN the record? a shared figure / date / document number with a cell or the adverse paper, else ≥ 3 shared
    content tokens with one cell (m1_spec_assembler._pleads). Deterministic, substance not form."""
    figs = set(_FIG.findall(paragraph))
    for t in record_texts:
        if figs and figs & set(_FIG.findall(t)):
            return True
    return any(len((S._tok(paragraph) - S._BOILER) & S._tok(t)) >= 3 for t in record_texts if len(t) < 4000)


# ATTRIBUTION CHECK (2026-11-14, from the skeptic's po-14 review): "Compl. ¶n pleads <X>" is a claim about ¶n's own words. Every figure,
# date and dollar amount in such a sentence must appear in adv[n] itself — not merely somewhere in the record. The skeptic caught
# "Compl. ¶3 pleads default as of December 1, 2025 / $166,499.98 / payable to Example Lender" (none in ¶3) three runs in a row.
_ATTR_RX = re.compile(r"Compl\.\s*¶\s*(\d{1,3})\b\s*(?:pleads|avers|alleges|states|recites|identifies|demands|lists|says)\b((?:(?!Compl\.\s*¶|Aff\.|Affidavit|Ex\.\s)[^.;])*)", re.I)
_FIG_RX = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?|\b(?:19|20)\d{2}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/(?:19|20)\d{2}\b|\b\d{2}-\d{2}-(?:19|20)\d{2}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\b|\b\d{6,}\b")
_NOUN_RX = re.compile(r"\b(?:[A-Z]{2,}(?:-[A-Z0-9]+)?|[A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,})*)\b")
_NOUN_STOP = {"PLAINTIFF", "DEFENDANT", "DEFENDANTS", "PLAINTIFFS", "COMPLAINT", "COMPL", "EXHIBIT", "EX", "COURT", "RULE", "THE", "THIS", "THAT",
              "PENNSYLVANIA", "PHILADELPHIA", "COUNTY", "COMMON", "PLEAS", "PA", "DEFENDANT'S", "PLAINTIFF'S", "PARAGRAPH", "SECTION", "COUNT",
              "WHEREFORE", "NOTE", "MORTGAGE", "PROPERTY", "PREMISES", "ACTION", "JUDGMENT", "AMOUNT", "DATE", "AFFIDAVIT", "ORDER", "RECORD"}
# the ground statement is DEFENDANT'S (1028(b)); the complaint never 'pleads' our ground. Argument words belong in the brief, not a pleading ¶.
_GROUND_MISATTR_RX = re.compile(r"^\s*(?:The\s+)?(?:Complaint|Compl\.|Plaintiff(?:'s)?\s+(?:pleading|complaint))\s+(?:pleads|avers|alleges|states|asserts)\s+(?:a\s+|the\s+)?(?:lack of|failure|insufficien|non-?conform|pendency|legal insufficiency|improper)", re.I)
_ARGUMENT_RX = re.compile(r"\b(?:shall be sustained|must be sustained|should be sustained|constituting (?:a|an)\b|is therefore (?:void|fatal)|it follows that|clearly|obviously)\b", re.I)
_SOURCE_RX = re.compile(r"Aff(?:idavit)?\.?\s*¶|\bEx\.\s*[A-Z0-9]|\bExhibit\s+[A-Z0-9]|\bdocket\b|Compl\.\s*¶|\btranscript\b|\b1099-C\b|\bForm\s+\d|\brecord(?:ed)?\s+(?:at|in|on)\b|\bInstrument\s*(?:No\.|#)|\[corpus:|\[official|\[doc\]", re.I)
_META_RX = re.compile(r"\b(?:this block|this unit|unit \d+|the drafter|drafting rule|as instructed|per the instructions|the cells?\b|RULE TEXT)", re.I)


def _record_cell(c):
    """a cell whose STATUS is RECORD (sworn affidavit / docket / exhibit fact the room verified) → its dict; else None. Agnostic: status, not text."""
    if isinstance(c, str) and c.lstrip().startswith("{"):
        try:
            d = json.loads(c)
            if isinstance(d, dict) and str(d.get("status", "")).upper() == "RECORD": return d
        except Exception:
            return None
    if isinstance(c, str) and c.startswith("CARD:") and "| RECORD |" in c:
        return {"record_facts": [c.split("PROPOSITION:", 1)[-1].strip()]}
    return None


_LAW_RX = re.compile(r"§\s*\d|Am\.\s*Jur\.|Pa\.R\.C\.P\.|Pa\.C\.S\.|U\.S\.C\.|C\.F\.R\.|P\.S\.\s*§|Black.s Law|\[corpus:", re.I)
_REC_RX = re.compile(r"Compl\.\s*¶|Exh\.\s*[A-Z]\b|¶\s*\d|\[doc:|1099-C|DOC\s*\d{6,}", re.I)
_BOT_RX = re.compile(r"[\"']bot[\"']:\s*[\"']([^\"']+)[\"']")
_FIND_RX = re.compile(r"'finding':\s*'(.{0,160})", re.S)


def _cell_meta(c):
    """(bot, finding, text-for-scoring) — cells are JSON lines; fall back to the raw string when they are not."""
    try:
        d = json.loads(c)
        return str(d.get("bot") or "?"), str(d.get("finding") or "")[:160], c
    except Exception:
        return (_BOT_RX.search(c) or [None, "?"])[1], ((_FIND_RX.search(c) or [None, c[:160]])[1]), c


def _stub_exhibits(out_path):
    """Exhibit lines from the stub's NEW EVIDENCE BLOCK ('### L — TITLE'), the manifest assemble_packet reads; [] when absent."""
    try:
        op = Path(out_path); stub = op.with_name(op.name.replace("_DRAFT", ""))
        if not stub.exists(): return []
        blk = stub.read_text(encoding="utf-8", errors="ignore").split("## NEW EVIDENCE BLOCK", 1)
        if len(blk) < 2: return []
        rows = re.findall(r"^### ([A-Z]) — (.+?)\s*(?:_\(record\)_)?\s*$", blk[1], re.M)
        return [f"Exhibit {L} — {t.strip()}" for L, t in rows]
    except Exception:
        return []


def _auth_keys(text):
    """comparable keys for the authorities a text names: § numbers (bare + pinned), Rule numbers, 'Am. Jur.' title+§, Black's."""
    ks = set()
    for m in re.finditer(r"§+\s*(\d{3,5}(?:\.\d+)?)((?:\([a-z0-9]\))*)", text): ks.add("s" + m.group(1)); ks.add("s" + m.group(1) + m.group(2))
    for m in re.finditer(r"(?:Rule|R\.\s?C\.\s?P\.|R\.\s?Civ\.\s?P\.|Civ\. ?R\.)\s*\*?(\d{3,4})", text): ks.add("r" + m.group(1))
    for m in re.finditer(r"(\d+[A-Z]?)\s+Am\.? ?Jur\.? ?2d\s+([A-Za-z ,&]+?)\s+§+\s*(\d+)", text): ks.add("aj" + re.sub(r"\W", "", m.group(1) + m.group(2)).lower() + m.group(3))
    if re.search(r"Black'?s", text, re.I): ks.add("blacks")
    return ks


def _clean_cite(c):
    c = re.split(r"\s*\[|\s*[“\"]", c.strip())[0].strip(" ;,.")
    return c


def _brief_required_authorities(cell_block, top=None):
    """the authorities of each LANE's top-N digests (N = M1_BRIEF_GUARD_TOP, default 4) — cleaned, deduped by cite key; this is the
    MUST-ARGUE list the brief prompt states and the guard checks (deterministic list → model comprehension → deterministic check)."""
    top = top or int(os.environ.get("M1_BRIEF_GUARD_TOP", "12"))
    req, seen, rank = [], set(), {}
    for line in cell_block.splitlines():
        lm = re.match(r"\[\d+\|unit [^|\]]+\|lane ([^\]]+)\]", line) or re.match(r"\[\d+\|unit ([^\]]+)\]", line)
        if not lm: continue
        ln = lm.group(1); rank[ln] = rank.get(ln, 0) + 1
        if rank[ln] > top: continue
        am = re.search(r"AUTH: ([^\n]+)", line)
        if not am: continue
        for piece in am.group(1).split(";"):
            cite = _clean_cite(piece.split(":")[0])
            if not cite or "NOT IN CORPUS" in cite or len(cite) > 60 or cite[:1].islower() or cite.startswith("The "): continue
            ks = frozenset(_auth_keys(cite))
            if not ks or ks & seen: continue
            seen |= ks; req.append(cite)
    return req


def _brief_missing_authorities(brief_txt, cell_block, top=None):
    """S-brief-carries-the-law (po-16, deterministic GUARD): every MUST-ARGUE authority absent from the brief, by cite key."""
    have = _auth_keys(brief_txt)
    return [c for c in _brief_required_authorities(cell_block, top) if not (_auth_keys(c) & have)]


def _brief_cell_block(cells, by_unit, budget=None, groups=None):
    """Law-first digests for the BRIEF, round-robin across LANES. groups = [[units of objection 1], [units of objection 2], …] →
    one lane per OBJECTION (its units' ranked cells merged, best first); without groups one lane per unit. po-16: 21 pleaded units on
    one 40K budget gave the model ~2 cells per unit and the dispositive unit's multi-authority cells never reached the window —
    per-objection lanes give each objection ~1/6 of the budget. Deterministic; the model still decides what to argue."""
    budget = budget or int(os.environ.get("M1_BRIEF_CELL_CHARS", "90000"))
    per = int(os.environ.get("M1_BRIEF_CELL_CHARS_EACH", "1000"))
    if groups:
        lanes = {}
        for gi, units in enumerate(groups):
            ks = [k for u in units for k in by_unit.get(u, [])]
            if ks: lanes[gi] = _rank_cells(cells, ks)
    else:
        lanes = {u: list(ks) for u, ks in sorted(by_unit.items()) if ks}
    unit_of = {k: u for u, ks in by_unit.items() for k in ks}
    # S-brief-law-diversity (po-16): within a lane, a cell whose authorities are ALL already shown yields to the first cell that adds a
    # new one (11 digests of the FIFTH were 1029(b)/1019(i)/1028(a) six ways while § 3204 / § 3308 / § 1201 / Rule 2002 cells — newer,
    # demoted OPEN by the record-pointer guard, so 3 points lower — never surfaced). Repeats are appended only after every new
    # authority in the lane has been seen once.
    def _keys_of(k):
        try: d = json.loads(cells[k])
        except Exception: return set()
        return set().union(*(_auth_keys(str(a.get("cite") or "")) for a in (d.get("authorities") or []) if isinstance(a, dict))) | _auth_keys(str(d.get("proposition") or "")[:400])
    for u in list(lanes):
        seen, fresh, rest = set(), [], []
        for k in lanes[u]:
            ks = _keys_of(k)
            if ks and ks - seen: fresh.append(k); seen |= ks
            else: rest.append(k)
        lanes[u] = fresh + rest
    out, used = [], 0
    while lanes and used < budget:
        for u in list(lanes):
            if not lanes[u]: lanes.pop(u); continue
            k = lanes[u].pop(0); d = _cell_digest(cells[k], per)
            if used + len(d) > budget: lanes.clear(); break
            out.append(f"[{k}|unit {unit_of.get(k, '?')}|lane {u}] {d}"); used += len(d) + 1
    return "\n".join(out)


def _cell_digest(c, budget=None):
    """What the composer READS of a cell. Cells are 10-17 KB JSON; the old c[:700] showed the JSON head (claim/title/bot/finding)
    and cut the law mid-sentence — the proposition (rule quote + violation), the verified authorities and the record facts never
    reached the model, so the draft could not plead what the room had grounded (po-16: 13 unit-2 cells carried § 3204 / real party
    in interest; the draft carried none). The digest puts LAW and RECORD first, bounded by M1_CELL_CHARS (default 1400)."""
    budget = budget or int(os.environ.get("M1_CELL_CHARS", "1400"))
    try:
        d = json.loads(c)
    except Exception:
        return c[:budget]
    auth = d.get("authorities") or []
    if isinstance(auth, list):
        auth = "; ".join(f"{a.get('cite')}: \"{str(a.get('quote') or '')[:220]}\"" for a in auth if isinstance(a, dict) and a.get("cite"))
    rec = d.get("record_facts") or ""
    if isinstance(rec, list): rec = " | ".join(str(x) for x in rec)
    parts = [f"[{d.get('bot', '?')}] {str(d.get('finding') or '').strip()}",
             f"LAW: {str(d.get('proposition') or '').strip()}",
             f"AUTH: {auth}" if auth else "",
             f"RECORD: {re.sub(r'[*`]', '', str(rec)).strip()}" if rec else ""]
    out, left = [], budget
    for i, ptxt in enumerate(x for x in parts if x):
        share = left if i == 3 else min(left, max(300, int(budget * (0.12, 0.5, 0.18, 0.2)[min(i, 3)])))
        if not ptxt or left <= 0: continue
        seg = ptxt[:share]; out.append(seg); left -= len(seg)
    return " || ".join(out)


def _rank_cells(cells, keys):
    """Deterministic order for one unit's cells: score (law + record + GROUNDED) desc, then round-robin across bots, duplicates
    (same bot, same first 160 chars of finding) dropped. Pure function of the cell text — no model, no table of topics."""
    seen, scored = set(), []
    for k in keys:
        bot, fnd, c = _cell_meta(cells[k])
        sig = (bot, re.sub(r"\s+", " ", fnd).lower())
        if sig in seen: continue
        seen.add(sig)
        # score: law present (3) + record pinned (2) + GROUNDED (1) + distinct cited sections (≤3); ties → NEWEST cell first (a later
        # run is the re-queue on corrected framing — its cells supersede the first pass, never the reverse)
        sc = (3 if _LAW_RX.search(c) else 0) + (2 if _REC_RX.search(c) else 0) + (1 if ("GROUNDED" in c and os.environ.get("M1_RANK_STATUS", "1") == "1") else 0) \
             + min(3, len(set(re.findall(r"§\s*\d[\d.()a-z-]*|Rule\s+\d{3,4}", c))))
        sc += min(3, c.count('"cite": "') - c.count('"cite": ""'))   # S-multi-authority: a card carrying 2-3 verified authorities outranks a one-cite card
        scored.append((sc, bot, k))
    lanes = {}
    for sc, bot, k in sorted(scored, key=lambda t: (-t[0], -t[2])):
        lanes.setdefault(bot, []).append(k)
    best = {b: max(s for s, bb, _ in scored if bb == b) for b in lanes}
    out, order = [], sorted(lanes, key=lambda b: (-best[b], b))
    while any(lanes.values()):
        for b in order:
            if lanes[b]: out.append(lanes[b].pop(0))
    return out


def _record_figures(c):
    """$ amounts and dates carried by a RECORD cell's record_facts (fallback: finding) — the figures a block MUST plead."""
    d = _record_cell(c)
    if not d: return []
    txt = " ".join(d.get("record_facts") or []) or str(d.get("finding") or "")
    return [f for f in _FIG_RX.findall(txt) if "$" in f or "-" in f or "/" in f]


def _record_sentences(c):
    """the RECORD cell's own sentences (record_facts), pleading-shaped — used only when the model would not plead the figures."""
    d = _record_cell(c) or {}
    out = []
    for t in d.get("record_facts") or []:
        t = re.sub(r"^RECORD FACT \(sworn[^)]*\):\s*", "", str(t)).split(" Vehicle:")[0].strip()
        if t:
            t = t if t.endswith(".") else t + "."
            if not _SOURCE_RX.search(t):   # the affidavit's own words carry the affidavit as source (po-15 ¶70: a record fact with no cite)
                t = t[:-1] + " (Affidavit under Pa.R.C.P. 1028(c)(2))."
            out.append(t)
    return out


def _attribution_bad(p, adv_map):
    out = []
    for m in _ATTR_RX.finditer(p):
        n = int(m.group(1)); tail = m.group(2)
        src = _n(adv_map.get(n, "")) if isinstance(adv_map, dict) else ""
        if not src: continue
        for f in _FIG_RX.findall(tail):
            f_n = _n(f).replace(",", "")
            if f_n not in src.replace(",", ""):
                out.append(f"attributes '{f}' to Compl. ¶{n} but ¶{n} does not contain it — cite the ¶ that does, or state it as a record fact")
        # po-15 (2026-11-14): 6 of 11 skeptic rejections were NOUN facts attributed to a ¶ that never names them ('Compl. ¶11 pleads …
        # the Note or FHA guarantee' — ¶11 identifies Plaintiff only). Proper nouns, acronyms and defined terms in the tail must be in ¶n.
        for w in _NOUN_RX.findall(tail):
            if w.upper() in _NOUN_STOP or w.lower() in src:
                continue
            out.append(f"attributes '{w}' to Compl. ¶{n} but ¶{n} never names it — cite the ¶ that does, or plead it as Defendant's record fact with its source")
    return out


def _check_paras(paras, adv_paras, corpora, need_cite=True, rule_corpora=None, grounds_text=None, record_texts=None, adv_map=None):
    """Pleading-paragraph guardrail (1019(a)/1022 shape): a paragraph that QUOTES must quote verbatim from the rule text, a cell or the
    adverse paper; a paragraph that does not quote must CITE what it rests on (Compl. ¶n / Compl. Ex. / a record fact). The objection as a
    whole must quote the governing rule verbatim at least once (1028(b): the ground stated specifically — in the rule's words)."""
    bad = []
    if not isinstance(paras, list) or not paras or not all(isinstance(p, str) and p.strip() for p in paras):
        return ["not a JSON list of non-empty strings"]
    cited = rule_quoted = False
    rule_hay = _n("\n".join(rule_corpora or []))
    for i, p in enumerate(paras, 1):
        bad += [f"¶{i}: {b}" for b in _adv_cites_ok(p, adv_paras)]
        spans = QUOTE_SPAN.findall(p)
        has_cite = bool(ADV_CITE.search(p) or re.search(r"\bEx\. [A-Z]|\[corpus:|\[doc\]|\[official", p, re.I)) \
            or _in_record(p, record_texts or [])      # grounded when the RECORD carries it — never by a "Record Fact" label
        states_ground = bool(grounds_text) and _states(p, grounds_text)   # the 1028(b) 'ground stated specifically' ¶ (≥5 consecutive words of the ground)
        if spans:
            bad += [f"¶{i}: {b}" for b in _quotes_verbatim(spans, corpora)]
            if any(len(sp.split()) >= 6 and all(_n(pc) in rule_hay for pc in _pieces(sp)) for sp in spans):
                rule_quoted = True
        elif not has_cite and not states_ground and not RULE_CITE.search(p) and not re.search(r"incorporat|foregoing", p, re.I):
            bad.append(f"¶{i}: not in the record — no cell or Complaint paragraph carries this fact (its figures/dates/names match nothing given to you); cite the Compl. ¶n it rests on, quote the rule, or delete it: “{p[:160]}”")
        if BANNED.search(p):
            bad.append(f"¶{i}: banned label '{BANNED.search(p).group(0)}'")
        if states_ground and _GROUND_MISATTR_RX.search(p):
            bad.append(f"¶{i}: the ground is attributed to the complaint ('{p[:60]}…') — the ground is Defendant's: write 'Defendant objects on the ground of \"<rule text>\"' and then the facts")
        if _ARGUMENT_RX.search(p):
            bad.append(f"¶{i}: argument word '{_ARGUMENT_RX.search(p).group(0)}' — a pleading ¶ states a fact or the rule; the conclusion belongs in the WHEREFORE / brief")
        if has_cite and not spans and not ADV_CITE.search(p) and _in_record(p, record_texts or []) and not _SOURCE_RX.search(p):
            bad.append(f"¶{i}: record fact without its source — add where it sits (Aff. ¶n / Ex. X / the docket / the transcript)")
        if adv_map:
            bad += [f"¶{i}: {b}" for b in _attribution_bad(p, adv_map)]
        if _META_RX.search(p):
            bad.append(f"¶{i}: drafting meta-language '{_META_RX.search(p).group(0)}' — a pleading paragraph states a fact or the rule, never the drafting instruction")
        if has_cite:
            cited = True
    if need_cite and not cited:
        bad.append("no paragraph cites the adverse paper (Compl. ¶n / Compl. Ex.) or a record fact")
    if rule_corpora and not rule_quoted:
        bad.append("no paragraph quotes the governing rule verbatim (≥ 6 consecutive words in quotation marks from RULE TEXT)")
    return bad


# ------------------------------------------------------------------ assembly
def assemble(court, kind, ftype, side, adverse_path, cells_paths, out_path, case_id, llm_url, decision=None, title=None):
    adverse = Path(adverse_path).read_text(encoding="utf-8", errors="replace")
    adv = S.numbered_paragraphs(adverse)
    if not adv:
        return {"ok": False, "stop": "no numbered paragraphs in the adverse paper"}
    adv_paras = set(adv)
    cells = S.load_cells(cells_paths)
    prof = _profiles().get(court, {}).get(kind, {})
    secs = [s for s in (prof.get("mandatory_sections") or []) if s.get("heading")]
    atts = prof.get("mandatory_attachments") or []
    # the governing rule: from the instrument card ("RULE THAT MAKES IT THIS CLASS … <cite>"), else the first cite the profile names
    gov = None
    if decision and decision.get("line"):
        m = re.search(r"(Pa\.R\.C\.P\.\s*\d{3,4}|Pa\.R\.A\.P\.\s*\d{3,4})", decision["line"])
        gov = m.group(1) if m else None
    cites = ([gov] if gov else []) + [re.sub(r"\(.*$", "", str(s.get("rule") or "")).strip() for s in secs]
    # + the forum_rules.json cell for this court/filing type (DATA: the rules that govern a pleading of this kind in this forum;
    #   same cell the room grounds on) — the LLM may quote a rule ONLY if its verbatim text is in this pool
    try:
        _fr = json.loads(Path("scripts/rooms/forum_rules.json").read_text(encoding="utf-8")).get(court, {})
        cites += [str(r.get("cite")) for r in (_fr.get(ftype) or _fr.get("_default") or []) if r.get("cite")]
    except Exception:
        pass
    cites = [re.sub(r"\s+", " ", c) for c in cites if re.match(r"(Pa\.R\.C\.P\.|Pa\.R\.A\.P\.|Phila\.\s*Civ\.\s*R\.)", c or "")]
    rules, missing = rule_pool(cites)
    gov = gov if gov in rules else next((c for c in rules if c.startswith("Pa.R.C.P.")), None)
    side_rec = {"kind": kind, "class": (decision or {}).get("class"), "governing_rule": gov, "rules_not_in_corpus": missing,
                "objections": [], "sections": [], "fail": [], "open": []}
    if not gov:
        side_rec["fail"].append("governing rule text not in corpus — nothing verbatim to ground the pleading on")
        Path(out_path).with_suffix(".profile_sidecar.json").write_text(json.dumps(side_rec, indent=1), encoding="utf-8")
        return {"ok": False, "fail": side_rec["fail"]}
    grounds = grounds_in_rule(rules[gov])
    corpora = list(rules.values()) + cells + [adverse]      # the adverse paper's own words are quotable — they are the record
    rule_block = "\n\n".join(f"[{c}]\n{t[:3000]}" for c, t in rules.items())
    # S-cell-digest-brief (po-16, sibling of the pleading fix): the BRIEF / mandatory sections read this block, and it was the first 420
    # chars of EVERY cell in file order, cut at 12 000 chars downstream — ≈ 28 JSON heads, no proposition, no authority, so the brief
    # argued from record facts alone with zero law. Same class fix: ranked, law-first digests, bounded by M1_BRIEF_CELL_CHARS.
    cell_block = None   # built after by_unit (below): every unit's best law-bearing cells, round-robin
    adv_block = "\n".join(f"¶{n}: {adv[n][:200]}" for n in sorted(adv))[:9000]

    # ---- 1. PLAN = the ROOM's GROUND cards (user-locked 2026-11-14: the bots decide the ground per unit, the skeptic grades it there;
    #   m1 only drafts). No LLM planner here — that was a second comprehension layer over the same question and it drifted run to run.
    maxn = int(os.environ.get("M1_MAX_OBJECTIONS", "12"))
    cap_cells = int(os.environ.get("M1_CELLS_PER_OBJECTION", "40"))
    import glob as _glob
    _b = next((pp for pp in Path(out_path).resolve().parents if (pp / "m2").is_dir()), None)
    gcards = sorted(_glob.glob(str(_b / "m2" / "room_r*_cards.md"))) if _b else []
    decisions = S.ground_decisions(gcards)
    if not decisions:
        side_rec["fail"].append("no SUSTAINED GROUND cards in the bundle (m2/room_r*_cards.md) — the room's GROUND ROUND decides the "
                                "enumerated ground per unit; m1 does not choose grounds")
        Path(out_path).with_suffix(".profile_sidecar.json").write_text(json.dumps(side_rec, indent=1), encoding="utf-8")
        return {"ok": False, "fail": side_rec["fail"]}
    _unit_rx = re.compile(r"hypothesis states:\s*(\d{1,3})\.", re.I)
    by_unit = {}
    for i, c in enumerate(cells):
        m = _unit_rx.search(c)
        if m: by_unit.setdefault(int(m.group(1)), []).append(i)
    # S-cell-rank (class fix, po-16 2026-11-15): the per-unit cap used to take the FIRST n cells in file order, so a unit's law-bearing
    # cells (Rule 2002 / § 3204 / Am. Jur. — the real-party-in-interest chain) sat past the cut while 85 cells carried them. The cap
    # is kept (bounded piece); what passes it is RANKED deterministically — cells that quote LAW and pin the RECORD first, then
    # round-robin across bots so one lane's near-duplicates cannot crowd the rest out; identical findings collapse to one.
    by_unit = {u: _rank_cells(cells, ks) for u, ks in by_unit.items()}
    cell_block = None   # built once the plan is known (below): only the units actually pleaded feed the brief
    support = {}
    for d in decisions:
        g = d["ground"]
        if not g: continue
        if g not in grounds:
            side_rec["open"].append(f"unit {d['unit']}: card ground ({g}) is not worded in {gov} — unit not drafted"); continue
        sp = support.setdefault(g, {"cells": [], "units": [], "titles": []})
        sp["units"].append(d["unit"]); sp["titles"].append(d["title"])
        sp["cells"] += [x for x in by_unit.get(d["unit"], []) if x not in sp["cells"]]
    plan = []
    for g in grounds:                                   # rule order = the pleading's order
        sp = support.get(g)
        if not sp: continue
        ap = sorted({n for n in adv_paras if any(re.search(rf"Compl\.\s*¶\s*{n}\b", cells[k]) for k in sp["cells"][:60])})
        plan.append({"ground": g, "heading": grounds[g], "adverse_paras": ap, "cells": sp["cells"][:cap_cells],
                     "cells_total": len(sp["cells"]), "units": sp["units"], "unit_titles": sp["titles"],
                     "unit_cells": {u: by_unit.get(u, [])[:cap_cells] for u in sp["units"]}})
        if not sp["cells"]:
            side_rec["open"].append(f"ground ({g}) units {sp['units']}: no cells carry these units — drafted from the card + adverse paper only")
    side_rec["plan_coverage"] = {"cells": len(cells), "ground_cards": len(decisions),
                                 "units_with_ground": [d["unit"] for d in decisions if d["ground"]],
                                 "units_none": [d["unit"] for d in decisions if not d["ground"]],
                                 "grounds_supported": [p["ground"] for p in plan], "cells_per_ground": {p["ground"]: p["cells_total"] for p in plan}}
    if not plan:
        side_rec["fail"].append("plan: GROUND cards name no enumerated ground of the governing rule")
    plan = plan[:maxn]

    # ---- 2. notice to plead: which grounds the FORUM rule says require an answer (from the local rule text, not a table)
    ntp_grounds, ntp_quote = set(), ""
    local = [c for c in rules if c.startswith("Phila")]
    if local:
        got = _llm(llm_url, SYS_NTP, "FORUM RULE TEXT:\n" + "\n\n".join(rules[c][:3000] for c in local) +
                   "\n\nLISTED GROUNDS: " + ", ".join(f"({n})" for n in grounds), max_tokens=400)
        if isinstance(got, dict):
            q = str(got.get("quote") or "")
            if q and not _quotes_verbatim([q], [rules[c] for c in local]):
                ntp_grounds = {str(x).strip("() ") for x in (got.get("grounds") or []) if str(x).strip("() ") in grounds}; ntp_quote = q
            else:
                side_rec["open"].append("notice-to-plead rule: quote not verbatim in the local rule — notice omitted; review")

    # ---- 3. each objection: numbered paragraphs (continuous numbering) + WHEREFORE
    ORD = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH", "EIGHTH", "NINTH", "TENTH"]
    body, n0 = [], 0
    t = title or f"{side.upper() if side else 'DEFENDANT'}'S {ftype.replace('_', ' ').upper()}"
    body += [f"## {t}", ""]
    if ntp_grounds & {p["ground"] for p in plan}:
        body += ["## NOTICE TO PLEAD", "", f"TO: Plaintiff — You are hereby notified to file a written response to the enclosed {ftype.replace('_', ' ')} "
                 f"within twenty (20) days from service hereof or a judgment may be entered against you. (\"{ntp_quote}\" {local[0]})", ""]
    body += ["## PRELIMINARY OBJECTIONS", "", f"Defendant, appearing sui juris, objects to Plaintiff's pleading under {gov} as follows.", ""]
    rule_quote_reg = {}   # normalized rule-quote text → ¶ number where first quoted (one verbatim quote per rule, cross-cite after)
    for i, p in enumerate(plan):
        pcells = [cells[k] for k in p["cells"]]
        # ONE BOUNDED CALL PER UNIT (user-locked 2026-11-14): a ground carrying 7 units (112 cells) cannot be drafted in one 3-8 ¶ call —
        # po-14 drafted the amount conflict and dropped FHA/verification/HUD/TILA/RESPA/MERS. Each unit = its own cells, its own title,
        # 2-5 ¶¶, each ¶ checked against the record exactly as before; the objection = the unit blocks in order + one WHEREFORE.
        paras, _tries = [], []
        unit_items = [(u, t, [cells[k] for k in p.get("unit_cells", {}).get(u, [])]) for u, t in zip(p["units"], p["unit_titles"])]
        unit_items = [(u, t, uc) for u, t, uc in unit_items if uc] or [(None, p["heading"], pcells)]
        per_lo, per_hi = (3, 8) if len(unit_items) == 1 else (2, 5)
        obj_rule_quoted = False
        rule_hay_all = _n("\n".join(rules.values()))
        for ui, (u, utitle, ucells) in enumerate(unit_items):
            uap = sorted({n for n in adv_paras if any(re.search(rf"Compl\.\s*¶\s*{n}\b", c) for c in ucells)}) or p["adverse_paras"]
            # sworn RECORD cells (1028(c)(2) affidavit) carry figures the block MUST plead — a capacity objection that omits the
            # $156,998 1099-C pair while its cells carry it is not comprehension, it is omission. Figures = $ amounts and dates.
            must = []
            for c in ucells:
                rf = _record_figures(c)
                if rf: must += rf
            must = sorted(set(must))
            got_ok, bad, last_good = None, [], None
            for attempt in (1, 2, 3):
                got = _llm(llm_url, SYS_PARAS.format(lo=per_lo, hi=per_hi),
                           (f"PREVIOUS ATTEMPT FAILED — fix exactly these and change nothing else: {bad}\n"
                            f"(When a date or amount is NOT in the Compl. ¶ you cite, do not write 'Compl. ¶n pleads <it>'; write it as Defendant's own "
                            f"record fact — 'The docket reflects…', 'Affidavit ¶n states…', 'The Note (Compl. Ex. C) is payable to…'.)\n\n" if bad else "") +
                           f"OBJECTION: {p['heading']} — ground ({p['ground']}) \"{grounds[p['ground']]}\" {gov}\n"
                           + (f"THIS BLOCK = unit {u}: {utitle}. Plead ONLY this unit's facts under this ground; other units of the same ground are "
                              f"drafted in their own blocks{' (do not repeat the incorporation sentence)' if ui else ''}.\n" if u is not None else "")
                           + (f"EVERY sworn RECORD FACT cell below must be pleaded (its dates and amounts must appear): {must}\n" if must else "")
                           + f"ADVERSE PARAGRAPHS YOU MAY CITE (ONLY these numbers exist):\n" + "\n".join(f"Compl. ¶{n}: {adv[n][:500]}" for n in uap)
                           + f"\n\nCELLS:\n" + "\n".join("- " + _cell_digest(c) for c in ucells) + f"\n\nRULE TEXT:\n{rule_block[:9000]}", max_tokens=1800)
                if isinstance(got, list):
                    got = [re.sub(r"^\s*(?:¶\s*\d+\s*[:.)]\s*|\d{1,3}\.\s+)", "", x) if isinstance(x, str) else x for x in got]
                    got = [re.sub(r"\s*(?:,\s*)?(?:as stated in the RECORD FACT|RECORD FACT|Record Fact|record:\s*Point\s*\d+)\.?", "", x).strip()
                           if isinstance(x, str) else x for x in got]
                    got = [re.sub(r"\s*\[(?:corpus|doc|official)[^\]]*\]", "", x).strip() if isinstance(x, str) else x for x in got]
                    if ui and got:   # only the first block of an objection opens with the incorporation sentence
                        got = [x for x in got if not re.match(r"^\s*(?:Defendant\s+)?(?:incorporates|The foregoing paragraphs are incorporated)", x, re.I)] or got
                # the governing rule is quoted ONCE per objection (1028(b)) — checked at objection level below, never per block
                bad = _check_paras(got, adv_paras, corpora + list(rules.values()), need_cite=bool(uap), rule_corpora=None,
                                   grounds_text=[grounds[p["ground"]]], record_texts=ucells + [adv[n] for n in uap] + cells, adv_map=adv)
                if isinstance(got, list) and must:
                    blob = _n(" ".join(got)).replace(",", "")
                    miss = [f for f in must if _n(f).replace(",", "") not in blob]
                    if miss: bad.append(f"sworn RECORD FACT figures not pleaded: {miss}")
                if not bad:
                    got_ok = got; break
                if isinstance(got, list) and got: last_good = (got, bad)
                _tries.append({"objection": i + 1, "unit": u, "attempt": attempt, "bad": bad, "paras": got if isinstance(got, list) else str(got)[:2000]})
            if got_ok is None and last_good:
                # GUARDRAIL PRUNE (never fabricate): drop only the paragraphs the record check named; keep the block if ≥1 fact ¶ survives
                got, bad = last_good
                badidx = {int(m.group(1)) for b in bad for m in [re.match(r"¶(\d+):", b)] if m}
                kept = [x for k, x in enumerate(got, 1) if k not in badidx]
                if must:   # the sworn facts the model would not plead are pleaded in the affidavit's own words — the record, not an inference
                    blob = _n(" ".join(kept)).replace(",", "")
                    for c in ucells:
                        rf = _record_figures(c)
                        if rf and any(_n(f).replace(",", "") not in blob for f in rf):
                            for sent in _record_sentences(c):
                                kept.append(sent); blob += " " + _n(sent).replace(",", "")
                kept = [x for x in kept if x and not _META_RX.search(x)]
                rebad = _check_paras(kept, adv_paras, corpora + list(rules.values()), need_cite=bool(uap), rule_corpora=None,
                                     grounds_text=[grounds[p["ground"]]], record_texts=ucells + [adv[n] for n in uap] + cells, adv_map=adv) if kept else ["empty"]
                if kept and not rebad and sum(1 for x in kept if not re.match(r"^\s*(?:Defendant\s+)?incorporat", x, re.I)) >= 1:
                    got_ok = kept
                    side_rec["open"].append(f"objection {i+1} ({p['ground']}) unit {u} '{utitle}': PRUNED {len(got)-len(kept)} ¶ after 3 attempts — {bad[:2]}")
            if got_ok is None:
                side_rec["open"].append(f"objection {i+1} ({p['ground']}) unit {u} '{utitle}': not drafted after 3 attempts — {bad[:3]}")
                continue
            if any(len(sp.split()) >= 6 and all(_n(pc) in rule_hay_all for pc in _pieces(sp)) for x in got_ok for sp in QUOTE_SPAN.findall(x)):
                obj_rule_quoted = True
            paras += got_ok
        if paras and not obj_rule_quoted:
            # 1028(b): the ground stated specifically — in the rule's own words. Verbatim rule text, deterministic, never an inference;
            # Defendant's statement (never 'the complaint pleads <our ground>'), placed where a ground is stated: at the head of the objection.
            g = f"Defendant objects on the ground of \"{grounds[p['ground']]}\" ({gov}(a)({p['ground']}))."
            at = 1 if re.match(r"^\s*(?:Defendant\s+)?(?:incorporates|The foregoing paragraphs are incorporated)", paras[0], re.I) else 0
            paras.insert(at, g)
        # ONE VERBATIM QUOTE PER RULE TEXT (user-locked 2026-11-15): a rule is quoted in full the first time it is relied on, anywhere in the
        # draft; every later reliance cross-cites that ¶ by number. Several rules attaching to one unit = several first quotes. Checked by
        # normalized quote text across objections (rule_quote_reg), so 1019(i) quoted under the Second is cross-cited under the Fourth.
        for j, q in enumerate(paras):
            for sp in QUOTE_SPAN.findall(q):
                if not (len(sp.split()) >= 6 and all(_n(pc) in rule_hay_all for pc in _pieces(sp))):
                    continue
                key = _n(sp)
                if key in rule_quote_reg:
                    k = rule_quote_reg[key]
                    paras[j] = q.replace(f"\u201c{sp}\u201d", f"the rule text quoted at ¶{k}").replace(f'"{sp}"', f"the rule text quoted at ¶{k}")
                    side_rec.setdefault("rule_cross_cites", []).append({"objection": i + 1, "para": n0 + j + 1, "quoted_at": k, "rule_text": sp[:80]})
                else:
                    rule_quote_reg[key] = n0 + j + 1
        if _tries: side_rec.setdefault("failed_attempts", []).extend(_tries)
        if not paras:
            side_rec["fail"].append(f"objection {i+1} ({p['ground']}): no unit block passed the record check"); continue
        wf = _llm(llm_url, SYS_WHEREFORE, f"GROUND ({p['ground']}) \"{grounds[p['ground']]}\" {gov}\nPARAGRAPHS:\n" + "\n".join(paras) +
                  f"\n\nRULE TEXT:\n{rules[gov][:3000]}", max_tokens=300)
        wf = wf if isinstance(wf, str) and wf.strip() and not BANNED.search(wf) else f"WHEREFORE, Defendant respectfully requests that this Preliminary Objection be sustained."
        if not wf.upper().startswith("WHEREFORE"): wf = "WHEREFORE, " + wf
        body += [f"## {ORD[i] if i < len(ORD) else str(i+1)} PRELIMINARY OBJECTION — {p['heading']} ({gov}(a)({p['ground']}))", ""]
        body += [f"{n0 + j + 1}. {q.strip()}" for j, q in enumerate(paras)]
        n0 += len(paras)
        body += ["", wf, ""]
        side_rec["objections"].append({"ground": p["ground"], "heading": p["heading"], "paras": len(paras), "cells": len(pcells), "adverse_paras": p["adverse_paras"], "units": p["units"]})
    pleading_md = "\n".join(body)
    # S-brief-pleaded-units (po-16): the brief argues the objections that were PLEADED — its cells are the pleaded units' cells
    # (ranked, law-first, round-robin), not every unit in the issue list; 24 units sharing one budget left each objection one cell.
    _pleaded = {u for o in side_rec["objections"] for u in (o.get("units") or [])}
    _groups = [list(o.get("units") or []) for o in side_rec["objections"]]
    cell_block = _brief_cell_block(cells, {u: ks for u, ks in by_unit.items() if u in _pleaded} or by_unit, groups=_groups if any(_groups) else None)
    _must = _brief_required_authorities(cell_block)

    # ---- 4. the court profile's MANDATORY sections (brief/memorandum, exhibits list, proposed order …) — each written to its
    #        own 'requires' sentence from the body + cells; guardrails: verbatim quotes, existing Compl. ¶, no attorney reference in an order
    extra = []
    for s in secs:
        H = str(s["heading"]).upper(); req = str(s.get("requires") or ""); rule = str(s.get("rule") or "")
        if any(k in H for k in ("VERIFICATION", "CERTIFICATE OF SERVICE")):
            continue   # the finisher owns the oath and the COS for the forum
        if H.startswith("EXHIBIT"):
            # S-exhibits-from-record (po-16): the stub's NEW EVIDENCE BLOCK (cells_to_stub: every [doc:] the room pinned, verified on
            # disk, lettered) IS the exhibit list assemble_packet builds from — the pleading must list the same letters, not the court
            # profile's generic attachments (which gave "A — Complaint" alone while the packet carried eight record documents).
            items = _stub_exhibits(out_path)
            if not items:
                items = [f"Exhibit A — Plaintiff's pleading objected to (the Complaint), as filed."]
                items += [f"Exhibit {chr(66 + k)} — {a.get('item') or a.get('label')}" for k, a in enumerate(a for a in atts if "complaint" not in str(a).lower()) if isinstance(a, dict)]
            extra += [f"## {H}", "", f"({rule}: {req})", ""] + [f"- {x}" for x in items] + [""]
            side_rec["sections"].append({"heading": H, "source": "profile attachments"}); continue
        txt, bad = None, []
        for attempt in ((1, 2, 3) if re.search(r"BRIEF|MEMORANDUM", H) else (1, 2)):
            got = _llm(llm_url, SYS_SECTION + (SYS_BRIEF_CONTRACT if re.search(r"BRIEF|MEMORANDUM", H) else ""),
                       (f"PREVIOUS ATTEMPT FAILED: {bad}\n\n" if bad else "") +
                       f"HEADING: {H}\nRULE: {rule}\nREQUIRES: {req}\n"
                       + ("MUST ARGUE OR EXPRESSLY DISTINGUISH — each of these authorities (from the top cells) appears in the brief by cite, under the objection its lane number names: " + "; ".join(_must) + "\n"
                          "OBJECTION → UNITS (the CELLS tagged [n|unit u|lane j] belong to objection j+1):\n" + "\n".join(
                           f"  {ORD[j] if j < len(ORD) else j + 1}: {o['heading'][:70]} → units {o['units']}" for j, o in enumerate(side_rec["objections"])) + "\n\n"
                          if re.search(r"BRIEF|MEMORANDUM", H) else "\n")
                       + f"PLEADING BODY ALREADY WRITTEN:\n{pleading_md[:12000]}\n\n"
                       f"CELLS:\n{cell_block}\n\nRULE TEXT:\n{rule_block[:9000]}", max_tokens=int(os.environ.get("M1_BRIEF_MAX_TOKENS", "10000") if re.search(r"BRIEF|MEMORANDUM", H) else os.environ.get("M1_SECTION_MAX_TOKENS", "6000")))
            if isinstance(got, list) and got and all(isinstance(x, str) for x in got):
                got = "\n\n".join(got)          # a list of paragraphs is the same section
            elif isinstance(got, dict) and not got.get("__error__"):
                _vals = [v for v in got.values() if isinstance(v, str) and v.strip()]      # {"section": "..."} / {"HEADING": "..."} — same text
                _lists = [x for v in got.values() if isinstance(v, list) for x in v if isinstance(x, str)]
                got = "\n\n".join(_vals + _lists) if (_vals or _lists) else got
            if not isinstance(got, str) or not got.strip():
                bad = [got.get("__error__") if isinstance(got, dict) and got.get("__error__") else f"not a JSON string ({type(got).__name__})"]; continue
            bad = _adv_cites_ok(got, adv_paras) + _quotes_verbatim(QUOTE_SPAN.findall(got), corpora)
            if BANNED.search(got): bad.append(f"banned label '{BANNED.search(got).group(0)}'")
            if "ORDER" in H and ATTY_REF.search(got): bad.append("proposed order carries an attorney reference")
            if re.search(r"BRIEF|MEMORANDUM", H):
                _miss = _brief_missing_authorities(got, cell_block)
                if _miss: bad.append(f"the brief omits authorities the CELLS carry — argue or expressly distinguish each: {'; '.join(_miss[:12])}")
            if not bad:
                txt = got; break
        if txt is None and re.search(r"BRIEF|MEMORANDUM", H) and isinstance(got, str) and got.strip():
            # S-brief-supplement (po-16): the last attempt is ONE or a few authorities short (run 6: 23 of 24 present, 1029(b) missing
            # thrice). Class rule: each missing authority gets its own paragraph, drafted from the cell that carries it, appended
            # under the objection whose lane the cell belongs to; then the same guard re-checks. Deterministic routing, model text.
            _miss = _brief_missing_authorities(got, cell_block)
            _other = [b for b in bad if not str(b).startswith("the brief omits authorities")]
            if _miss and len(_miss) <= int(os.environ.get("M1_BRIEF_SUPP_MAX", "4")) and not _other:
                supp = []
                for cite in _miss:
                    ks = _auth_keys(cite)
                    line = next((l for l in cell_block.splitlines() if ks & _auth_keys((re.search(r"AUTH: ([^\n]+)", l) or [None, ""])[1])), "")
                    lm = re.search(r"\|lane (\d+)\]", line); j = int(lm.group(1)) if lm else None
                    obj = side_rec["objections"][j]["heading"] if j is not None and j < len(side_rec["objections"]) else "the objection it supports"
                    para = _llm(llm_url, SYS_SECTION + SYS_BRIEF_CONTRACT,
                                f"HEADING: {H} — SUPPLEMENT\nWrite ONE paragraph (no heading) for the ARGUMENT on objection '{obj}' applying this authority, quoted verbatim "
                                f"from the cell with its cite, to the record facts the cell names: {cite}\n\nCELL:\n{line}\n\nRULE TEXT:\n{rule_block[:6000]}", max_tokens=900)
                    if isinstance(para, dict): para = "\n\n".join(v for v in para.values() if isinstance(v, str))
                    if isinstance(para, list): para = "\n\n".join(x for x in para if isinstance(x, str))
                    if isinstance(para, str) and para.strip() and not _quotes_verbatim(QUOTE_SPAN.findall(para), corpora) and not BANNED.search(para):
                        supp.append(f"**{(ORD[j] if j is not None and j < len(ORD) else '')} — supplemental authority, {cite}.** {para.strip()}")
                cand = got.rstrip() + ("\n\n" + "\n\n".join(supp) if supp else "")
                still = _brief_missing_authorities(cand, cell_block)
                side_rec.setdefault("brief_supplement", []).append({"missing": _miss, "added": len(supp), "still_missing": still})
                if not still and not _adv_cites_ok(cand, adv_paras): txt = cand
                else: bad = [f"the brief omits authorities the CELLS carry (after supplement): {'; '.join(still[:6])}"]
        if txt is None:
            side_rec["fail"].append(f"section {H}: {bad[:4]}"); continue
        extra += [f"## {H}", "", txt.strip(), ""]
        side_rec["sections"].append({"heading": H, "rule": rule, "chars": len(txt)})
    md = pleading_md + "\n\n" + "\n".join(extra)
    Path(out_path).write_text(md, encoding="utf-8")
    Path(out_path).with_suffix(".profile_sidecar.json").write_text(json.dumps(side_rec, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"ok": not side_rec["fail"], "objections": len(side_rec["objections"]), "sections": len(side_rec["sections"]),
            "open": len(side_rec["open"]), "fail": side_rec["fail"], "out": str(out_path)}


# ------------------------------------------------------------------ gate (deterministic; m1_filing_gate calls it for the profile lane)
def profile_gate(draft_text, court, kind, adverse_text=None):
    f = []
    low = draft_text.lower()
    # S-po-check-only-for-po-papers (2026-11-17): a reply brief that QUOTES the other side's "Preliminary Objections" is not a
    # preliminary-objections paper; the numbered-objection anatomy applies only when this paper IS one (kind or title says so).
    _is_po_paper = bool(re.search(r"preliminary_objection|prelim", str(kind or ""), re.I)) or \
        bool(re.search(r"(?mi)^#\s+.*preliminary objections?", draft_text))
    if _is_po_paper and not re.search(r"(?m)^## (FIRST|SECOND|THIRD|\d+) PRELIMINARY OBJECTION", draft_text):
        f.append("NO NUMBERED OBJECTION HEADING")
    for m in re.finditer(r"(?m)^## (?:[A-Z]+|\d+) PRELIMINARY OBJECTION — .*?\((Pa\.R\.C\.P\. \d+)\(a\)\((\d+)\)\)", draft_text):
        pass
    if re.search(r"(?m)^## [A-Z]+ PRELIMINARY OBJECTION", draft_text) and draft_text.count("WHEREFORE") < len(re.findall(r"(?m)^## [A-Z]+ PRELIMINARY OBJECTION", draft_text)):
        f.append("OBJECTION WITHOUT WHEREFORE")
    if adverse_text:
        adv = set(S.numbered_paragraphs(adverse_text))
        bad = sorted({int(m.group(1)) for m in ADV_CITE.finditer(draft_text) if m.group(1) and int(m.group(1)) not in adv})
        if bad:
            f.append(f"CITED Compl. ¶ NOT IN ADVERSE PAPER: {bad[:10]}")
    if BANNED.search(draft_text):
        f.append(f"BANNED LABEL: {BANNED.search(draft_text).group(0)}")
    po = re.search(r"(?ms)^## \[?PROPOSED\]? ORDER.*?(?=^## |\Z)", draft_text)
    if po and ATTY_REF.search(po.group(0)):
        f.append("PROPOSED ORDER references an attorney")
    return f
