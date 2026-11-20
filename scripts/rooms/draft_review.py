#!/usr/bin/env python3
"""draft_review — the SKEPTIC pass over the m1 DRAFT (class stage, any pleading/motion spec). Rooms grade cards; nothing graded the
draft — so 'does Compl. ¶96 plead what ¶117 says?' reached the user. Deterministic guardrail + LLM comprehension, per response:
  D1 every 'Compl. ¶n' cited exists in the complaint text (hard)            D2 figures ($, dates, numbers) in a response appear in the
  cited Compl. ¶¶, the adverse paper, or the cells (else flagged UNSOURCED)  L1 skeptic LLM: classification right (fact vs law vs writing),
  the cited ¶ supports the fact, the fact answers the averment (not a non sequitur). Writes <draft>.review.md; exit 2 on BLOCKING.
usage: draft_review.py <DRAFT.md> <complaint.txt> <adverse.md> <cells.jsonl> [--llm URL] [--strict]
"""
import sys, re, json, os, urllib.request
from pathlib import Path

RESP_RX = re.compile(r"^(\d{1,4})\. \(Defendant's ¶\d+: \"(.*?)\"\)\s+(ADMITTED|DENIED — specifically|CONCLUSION OF LAW[^.]*|WRITING[^.]*)\.\s*(.*)$")
CITE_RX = re.compile(r"Compl\. ¶\s*(\d{1,4})")
FIG_RX = re.compile(r"\$\s?[\d,]+(?:\.\d\d)?|\b\d{1,2}/\d{1,2}/\d{4}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}\b|\b\d{5,7}\b|\b(?:forty|thirty|twenty|sixty|fifty)\b|\b\d{1,3} (?:draws|months|days)\b")
CONDUCT_RX = re.compile(r"\b(acted|performed (?:all|its|each)|has (?:taken|made) no (?:action|statement|representation)|did not (?:engage|refuse|act|fail|breach)|took no action|complied with)\b", re.I)
SYS = ("You are the SKEPTIC grading a Pennsylvania reply to new matter paragraph by paragraph. For each item return a verdict: "
       "SUSTAINED (classification correct; every cited Compl. ¶ pleads the fact relied on; the fact answers the averment) / "
       "REJECTED (wrong classification — an averment of the pleader's own conduct answered as conclusion of law, or a quoted document answered as law; "
       "or, for a DENIED/WRITING response, no cited Compl. ¶ pleads the fact relied on; or the fact is a non sequitur to the averment) / OPEN (cannot verify "
       "from the texts given). STANDARD for a CONCLUSION OF LAW response: SUSTAINED when the adverse paragraph states law / recites an order / characterizes, "
       "and the cited Compl. ¶ identifies the count or claim that averment attacks — it need not plead a fact. AMOUNTS: a sum attributed to the defendant's "
       "own records/admission ('per Defendant's own draw records', 'Defendant's Answer ¶14') with the Complaint's damages/refund-demand paragraph cited is "
       "SUSTAINED — the Complaint pleads the demand; the figure quantifies it. "
       "Judge ONLY from the texts given — never from memory. Output JSON list [{\"n\":<¶>,\"verdict\":\"SUSTAINED|REJECTED|OPEN\",\"why\":\"<=25 words\"}]. No prose.")


MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def _norm(t):
    """dates to MM/DD/YYYY, drop $ and thousands commas, lower — so 'February 23, 2026' == '02/23/2026' and '$3,093.81' == '3093.81'"""
    t = re.sub(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), (\d{4})\b",
               lambda m: f"{MONTHS[m.group(1)]:02d}/{int(m.group(2)):02d}/{m.group(3)}", t)
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", lambda m: f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}", t)
    return t.replace("$", "").replace(",", "").lower()


def _retrieve(q, k=6):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from pipeline.m2 import analysis_runner as AR
        return [(h.get("source", "?"), (h.get("text") or "")[:500]) for h in AR._retrieve([q], [], k=k)]
    except Exception as e:
        return [("ERROR", str(e)[:160])]


def _authority_check(items, ap, forum):
    """returns {n: [(label, source, snippet), ...]}; label = RULE | SUBJECT | NOT IN CORPUS"""
    out = {}
    rule_hits = [(s, x) for s, x in _retrieve(f"{forum} denials: averments admitted when not denied specifically; general denial has the effect of an admission; deemed denied", 6)
                 if re.search(r"admitted when not denied|effect of an admission|deemed to be denied", x, re.I)]
    for it in items:
        their = ap.get(it["n"], "")
        if not (CONDUCT_RX.search(their) or it["kind"].startswith("CONCLUSION")):
            continue
        rows = [("RULE", s, x[:160]) for s, x in rule_hits[:2]] or [("NOT IN CORPUS", "rule text", "denial rule not retrieved")]
        m = CONDUCT_RX.search(their)
        if m:   # the conduct phrase itself (+ its object), e.g. "acted in good faith", "acted in a reasonable, careful, prudent and lawful manner"
            subj = re.split(r"[.,;]| and | that ", re.sub(r"\s+", " ", their[m.start(): m.start() + 90]))[0].strip()   # the bare conduct phrase
            qtxt = f"{forum.split()[0]}: whether a party {subj} is a question of fact for the fact-finder, fact specific depending on the party's conduct"
        else:   # conclusion-of-law response: the legal proposition's head
            # S-no-hardcode (2026-11-14): the party name stripped here was the literal
            # "Progressive" — one case's adverse party, invisible in every other case. The
            # adverse party's names come from the case profile (role words stay as role words).
            subj = re.sub(r"\b(" + _party_strip_alt() + r"|at all (?:material|relevant) times)\b",
                          "", their, flags=re.I)[:140]
            qtxt = f"{forum.split()[0]}: '{subj}' — statement of law, standard, or court order recital; conclusion of law need not be answered"
        hits = _retrieve(qtxt, 12)
        if forum.lower().startswith("pennsylvania"):   # forum family: drop other-state reporters (cap_nj_, cap_ny_, njac_, nj_court_...) — never other states' law as Pa. grounding
            hits = [(s, x) for s, x in hits if not re.match(r"^(cap_(?!pa_|us_)|nj|ny_|fl_|cal_|tx_)", s)]
        good = [(s, x) for s, x in hits if re.search(r"question of fact|fact specific|fact-finder|finder of fact|ultimate fact|factual (?:finding|question|matter)|question for the jury|conclusion of law|matter of law|depend on the conduct", x, re.I)]
        rows += [("SUBJECT", s, x[:160]) for s, x in good[:2]] or [("NOT IN CORPUS", "subject", "no fact/law characterization retrieved")]
        out[it["n"]] = rows
    return out


PLEAD_RX = re.compile(r"^(\d{1,4})\.\s+(.+)$")
ARTIFACT_RX = re.compile(r"\bRECORD FACT\b|\bRecord Fact\.?|\brecord:\s*Point\s*\d+|stated in the rule's own words|\[corpus:[^\]]*\]|\[doc\]|¶\s*\d+\s*:", re.I)
SYS_PLEAD = ("You are the SKEPTIC grading a Pennsylvania PLEADING (preliminary objections / complaint) paragraph by paragraph for the party "
             "filing it (the objecting DEFENDANT, sui juris). For each item return a verdict: SUSTAINED (a FACT paragraph attributes the fact to its "
             "source — 'Compl. ¶n' for the adversary's averments, a record document for ours — and the cited text carries it; a RULE paragraph quotes "
             "the rule verbatim with its cite; a GROUND paragraph states the enumerated ground and a consequence the rule allows) / REJECTED (the paragraph "
             "ADOPTS the adversary's assertion as the defendant's own fact — e.g. states the plaintiff's pleaded address as the defendant's 'last-known "
             "address' when the record shows another address of record; or asserts a fact no cited text carries; or is argument/characterization dressed "
             "as fact; or contains drafting artifacts — tags, labels, meta-sentences about the drafting rule; or the ground named does not fit the "
             "action type) / OPEN (cannot verify from the texts given). Judge ONLY from the texts given — never from memory. "
             "Output JSON list [{\"n\":<¶>,\"verdict\":\"SUSTAINED|REJECTED|OPEN\",\"why\":\"<=25 words\"}]. No prose.")


def paras(txt):
    out = {}
    for m in re.finditer(r"^\s*(\d{1,4})\.\s+(.*?)(?=^\s*\d{1,4}\.\s|\Z)", txt, re.M | re.S):
        n = int(m.group(1)); out.setdefault(n, re.sub(r"\s+", " ", m.group(2)).strip())
    return out


def llm(url, sys_p, user, model=None):
    """same caller the drafter uses (handles this model's think-blocks / fenced JSON); a parse failure is a REVIEW INCOMPLETE, never a pass"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pipeline.m1_spec_assembler import llm_json
    got = llm_json(url, sys_p, user)
    if not isinstance(got, list):
        raise ValueError(f"skeptic returned {type(got).__name__}, not a list")
    return got


def _model(url):
    try:
        return json.loads(urllib.request.urlopen(url.rstrip("/") + "/models", timeout=10).read())["data"][0]["id"]
    except Exception:
        return "default"


# ---- S-no-hardcode (2026-11-14): the adverse party's names come from the case profile, keyed by
# the case dir in the draft path (.../obsidian_sync/cases/<slug>/...). Unknown case -> role words.
_CASE_SLUG = None
_ALT = {}


def _party_strip_alt() -> str:
    key = _CASE_SLUG or ""
    if key not in _ALT:
        alt = ""
        if key:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                from pipeline.case_profile import profile as _cp
                alt = "|".join(re.escape(n) for n in (_cp(key).get("adverse_names") or []))
            except Exception:
                alt = ""
        _ALT[key] = ("Defendant|Plaintiff" + ("|" + alt if alt else ""))
    return _ALT[key]


def _slug_from_path(p: Path) -> str:
    parts = p.resolve().parts
    return parts[parts.index("cases") + 1] if "cases" in parts else ""


def main(a):
    draft, compl, adv, cells = [Path(x) for x in a[:4]]
    global _CASE_SLUG
    _CASE_SLUG = _slug_from_path(draft) or _slug_from_path(compl)
    url = a[a.index("--llm") + 1] if "--llm" in a else os.environ.get("M1_LLM_URL", "http://localhost:8000/v1")
    dtxt, ctxt, atxt = draft.read_text(errors="ignore"), compl.read_text(errors="ignore"), adv.read_text(errors="ignore")
    cell_txt = cells.read_text(errors="ignore") if cells.exists() else ""
    cf = next((c for c in list(compl.parents) + list(draft.parents) + list(adv.parents) if (c / "CASE_FACTS.md").exists()), None)   # the case dir
    facts_txt = _norm((cf / "CASE_FACTS.md").read_text(errors="ignore")) if cf else ""
    cp, ap = paras(ctxt), paras(atxt)
    if not cp:          # defendant's side: there is no complaint of ours — "Compl. ¶n" in the draft cites the ADVERSE paper
        cp = ap
    items, rows = [], []
    pleading = bool(re.search(r"^## .*PRELIMINARY OBJECTION|^## COUNT [IVX]+|^## OBJECTIONS?\b", dtxt, re.M))
    body = dtxt.split("## BRIEF", 1)[0] if pleading else dtxt      # the brief/order/COS are not numbered pleading ¶s
    for line in body.splitlines():
        m = RESP_RX.match(line.strip())
        if not m and pleading:
            pm = PLEAD_RX.match(line.strip())
            if pm and not re.match(r"^\d+\.\s+\S+\s+\[", line.strip()):   # skip Schedule A definition rows "1. term [Dict.] — …"
                n, text = int(pm.group(1)), pm.group(2)
                cites = [int(c) for c in CITE_RX.findall(text)]
                flags = []
                for c in cites:
                    if c not in cp: flags.append(f"BLOCKING D1: Compl. ¶{c} does not exist in the complaint text")
                src = _norm(" ".join(cp.get(c, "") for c in cites) + " " + cell_txt + " " + facts_txt)
                for f in set(FIG_RX.findall(text)):
                    key = _norm(f)
                    if key and key not in src: flags.append(f"UNSOURCED D2: '{f}' not in cited Compl. ¶¶ / cells / CASE_FACTS")
                if ARTIFACT_RX.search(text): flags.append(f"BLOCKING D4: drafting artifact in court-facing text: '{ARTIFACT_RX.search(text).group(0)}'")
                items.append({"n": n, "kind": "PLEADING ¶", "text": text, "cites": cites, "flags": flags, "plead": True})
            continue
        if not m:
            continue
        n, quote, kind, text = int(m.group(1)), m.group(2), m.group(3).split(" —")[0].strip(), m.group(4)
        cites = [int(c) for c in CITE_RX.findall(text)]
        flags = []
        for c in cites:
            if c not in cp: flags.append(f"BLOCKING D1: Compl. ¶{c} does not exist in the complaint text")
        src = _norm(" ".join(cp.get(c, "") for c in cites) + " " + ap.get(n, "") + " " + cell_txt)
        for f in set(FIG_RX.findall(text)):
            key = _norm(f)
            if key and key not in src:
                if key in facts_txt: flags.append(f"INFO D2: '{f}' is a CASE_FACTS record figure, not pleaded in the Complaint — must be attributed to its source in the response")
                else: flags.append(f"UNSOURCED D2: '{f}' not in cited Compl. ¶¶ / adverse ¶{n} / cells / CASE_FACTS")
        if kind.startswith("CONCLUSION") and CONDUCT_RX.search(ap.get(n, "") or quote): flags.append("BLOCKING D3: averment of the pleader's own conduct answered as conclusion of law (1029(b) admission risk)")
        items.append({"n": n, "kind": kind, "text": text, "cites": cites, "flags": flags})
    # A1 AUTHORITY (class, 2026-11-13): the classification rule itself must be grounded in the corpus, not asserted by the chat.
    # For every response that denies a CONDUCT averment and every CONCLUSION_OF_LAW response, pull the corpus (same retriever
    # m0/ground_pack use) for (a) the forum's denial rule text and (b) the fact/conclusion characterization of that averment's
    # subject; record chunk ids or NOT IN CORPUS. Review-only: it never rewrites the draft; a NOT IN CORPUS on the rule text blocks.
    authority = _authority_check(items, ap, os.environ.get("REVIEW_FORUM", "Pennsylvania Pa.R.C.P. 1029"))
    # LLM comprehension in batches of 4
    verdicts = {}
    for i in range(0, len(items), 4):
        b = items[i:i + 4]
        user = "\n\n".join(f"ADVERSE ¶{it['n']}: {ap.get(it['n'], '')[:700]}\nRESPONSE ({it['kind']}): {it['text'][:700]}\nCITED COMPLAINT ¶¶:\n" + ("\n".join(f"  Compl. ¶{c}: {cp.get(c, '(missing)')[:500]}" for c in it["cites"]) or "  (none)") for it in b)
        if any(it.get("plead") for it in b):
            user = "\n\n".join(f"PLEADING ¶{it['n']}: {it['text'][:700]}\nCITED COMPLAINT ¶¶:\n" + ("\n".join(f"  Compl. ¶{c}: {cp.get(c, '(missing)')[:500]}" for c in it["cites"]) or "  (none)") for it in b) \
                   + "\n\nDEFENDANT'S RECORD (CASE_FACTS, excerpt):\n" + facts_txt[:6000]
        try:
            for v in llm(url, SYS_PLEAD if any(it.get("plead") for it in b) else SYS, user):
                verdicts[int(v.get("n", 0))] = (str(v.get("verdict", "OPEN")).upper(), str(v.get("why", ""))[:200])
        except Exception as e:
            for it in b: verdicts[it["n"]] = ("OPEN", f"llm error: {e}"[:120])
    out = [f"# DRAFT REVIEW (skeptic over the draft) — {draft.name}", "", "| ¶ | kind | deterministic | skeptic | why |", "|---|---|---|---|---|"]
    auth_lines = ["", "## Authority check (classification rule grounded in corpus — [corpus:<source>] or NOT IN CORPUS)", ""]
    auth_block = False
    for n, rows in sorted(authority.items()):
        for lab, s, x in rows:
            auth_lines.append(f"- ¶{n} {lab}: [{'corpus:' + s if lab != 'NOT IN CORPUS' else s}] {x.replace(chr(10), ' ')}")
            if lab == "NOT IN CORPUS" and s == "rule text": auth_block = True
    blocking, hard = [], []
    for it in items:
        v, why = verdicts.get(it["n"], ("OPEN", "no verdict"))
        det = "; ".join(it["flags"]) or "ok"
        if any(f.startswith("BLOCKING") for f in it["flags"]): hard.append(it["n"])
        if any(f.startswith("BLOCKING") for f in it["flags"]) or v == "REJECTED": blocking.append(it["n"])
        out.append(f"| {it['n']} | {it['kind']} | {det} | {v} | {why.replace('|', '/')} |")
    out += auth_lines
    if auth_block: hard.append("AUTHORITY: denial rule text NOT IN CORPUS")
    incomplete = [it["n"] for it in items if verdicts.get(it["n"], ("OPEN", ""))[1].startswith("llm error")]
    tag = "DO NOT FILE" if blocking else ("REVIEW INCOMPLETE — skeptic did not grade" if (incomplete or not items) else "FILE")
    out += ["", f"**{tag}** — blocking ¶¶: {blocking or 'none'}; ungraded ¶¶: {incomplete or 'none'}; responses reviewed: {len(items)}"]
    rp = draft.with_suffix(".review.md"); rp.write_text("\n".join(out)); print("\n".join(out[-1:])); print(f"review -> {rp}")
    rej = {str(it["n"]): (verdicts.get(it["n"], ("", ""))[1] or "; ".join(it["flags"]))[:160] for it in items if it["n"] in blocking}
    draft.with_suffix(".redraft.json").write_text(json.dumps(rej, ensure_ascii=False))   # the chain feeds this to m1 as M1_REDRAFT
    # exit: 2 = deterministic BLOCKING (missing Compl. ¶, conduct-as-law) → chain STOPs; 3 = skeptic did not grade → chain STOPs;
    #       4 = skeptic REJECTED only (cite/cell choice) → chain runs ONE redraft loop, then WARNs and attaches the review to the packet; 0 = FILE
    return 2 if hard else (3 if incomplete else (4 if blocking else 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
