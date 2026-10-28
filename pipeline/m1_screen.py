#!/usr/bin/env python3
"""m1_screen — the SCREEN posture of the m1 rebuttal lane: the argument a court of appeals invites when it screens an
appeal itself (28 U.S.C. § 1915(e)(2) / 3d Cir. L.A.R. 27.4 + I.O.P. 10.6). Doctrine of record:
obsidian_sync/00_system/antifrivolous.md. Ported from the m2 sandbox (_sandbox_m2_screen_lane/screen_assembler.py) —
m2 analyses, m1 drafts.

There is no opposing brief: the unit is EVERY ORDER the appeal brings up (merged into the final judgment, FRAP
3(c)(4)). m1_rebuttal.assemble() drafts each point from the m2-bridged matrix/bank exactly as in any rebuttal; this
module only FRAMES that body. Its job is the guardrail, not the reasoning:

  I.   The test — verbatim from the doctrine note's corpus-verified table (Neitzke/Denton). Never paraphrased.
  II.  Every order, every error — m1's points. A point the LLM reads as arguing for the APPELLEE, or a point m1 could
       not draft (NOVEL), is not argued and is listed in the sidecar, never silently lost.
  III. Findings a frivolousness disposition must contain (Denton, 504 U.S. at 34): for each authority relied on,
       whether the panel holds it an "indisputably meritless legal theory", whether it reads the text other than it
       says, and which record fact is "clearly baseless".
  RELIEF. L.A.R. 27.4(b): refer to a merits panel and direct briefing.

Posture is decided by m0's stub (## POSTURE names the screening statute/rule) — the stub decides, m1 obeys.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, urllib.request
from pathlib import Path

ROOT = next((p for p in Path(__file__).resolve().parents if (p / "efiling").is_dir()), Path(__file__).resolve().parents[1])
DOCTRINE = Path(os.environ.get("M1_SCREEN_DOCTRINE", str(_eroot() / "obsidian_sync/00_system/antifrivolous.md")))
_TEST_CITES = {
    'Meaning of "frivolous"': "Denton v. Hernandez, 504 U.S. 25, 31 (1992) (quoting Neitzke v. Williams, 490 U.S. 319, 325 (1989))",
    "Legal frivolousness": "Neitzke, 490 U.S. at 327",
    "Losing ≠ frivolous": "Neitzke, 490 U.S. at 328",
    "Factual frivolousness": "Denton, 504 U.S. at 32-33",
    "No factfinding": "Denton, 504 U.S. at 32",
    "What review checks (the court's duties)": "Denton, 504 U.S. at 34",
}
# MERGER (scope of review): the notice designates the judgment; every interlocutory order rides with it. Verified
# verbatim [corpus:amjur2d_32A_volume_32a#ffff418347786dab]; body of § 1030 '—Prejudgment orders' (the '§ 1031' on that
# page is the running header), 32A Am Jur 2d (1982). Pre-empts 'that order is outside the notice'.
MERGER = ("\"a designation of the final judgment is sufficient to bring up for review all interlocutory orders merged "
          "in that judgment; it is unnecessary to specify the particular orders.\" 32A Am. Jur. 2d Federal Practice "
          "and Procedure § 1030 (1982)")
_SCREEN_SIGNS = re.compile(r"1915\(e\)\(2\)|L\.A\.R\.\s*27\.4|I\.O\.P\.\s*10\.6|summary action", re.I)
_CITE = re.compile(r"([A-Z][\w\.'&\- ]{1,60}(?:, (?:Inc|LLC|L\.L\.C|Co|Corp|Ltd|N\.A)\.?)? v\. [A-Z][\w\.'&\-, ]{2,60}?, No\. [\w\-:]+, \d{4} WL \d+"
                   r"|\d+\s+U\.S\.C\.\s*§+\s*[\w\-\(\)\.]+|\d+\s+C\.F\.R\.\s*§+\s*[\w\-\(\)\.]+|Fed\.\s*R\.\s*(?:Civ|App)\.\s*P\.\s*[\d\w\(\)]+"
                   r"|[A-Z][\w\.'&\- ]{2,60} v\. [A-Z][\w\.'&\-, ]{2,60}, \d+ (?:U\.S\.|F\.\dd|F\.\s?Supp\.(?: \dd)?|S\. ?Ct\.) \d+(?:, \d+)?(?: \([^)]{2,30}\))?)")


def stub_sections(stub_text: str) -> dict:
    out, cur = {}, None
    for ln in stub_text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m: cur = m.group(1).strip().upper(); out[cur] = []; continue
        if cur: out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def is_screen(stub_text: str) -> bool:
    return bool(_SCREEN_SIGNS.search(stub_sections(stub_text).get("POSTURE", "")))


def caption_of(stub_text: str) -> tuple[str, str]:
    """(caption line as m0 wrote it, appellate docket 'NN-NNNN' or '')."""
    cap = stub_sections(stub_text).get("CAPTION", "").strip()
    m = re.search(r"\bNo\.\s*(\d{2}-\d{3,5})\b", cap)
    return cap, (m.group(1) if m else "")


def title_of(stub_text: str) -> str:
    """The filing's name as m0 wrote it in the stub's H1 ('# STUB: <title> — <docket>'), upper-cased."""
    m = re.search(r"(?m)^#\s*(?:STUB:\s*)?(.+?)\s*(?:—|--|$)", stub_text)
    t = (m.group(1).strip() if m else "").rstrip(".")
    return t.upper() if t else ""


def final_dkt_of(stub_text: str) -> str:
    m = re.search(r"final judgment\s*\((?:ECF|Dkt\.?)\s*(?:No\.?\s*)?(\d{1,4})\)", stub_sections(stub_text).get("POSTURE", ""), re.I)
    return f"Dkt. No. {m.group(1)}" if m else ""


def not_argued_of(stub_text: str) -> list[tuple[str, str]]:
    """m0's '## NOT ARGUED' section: '- <heading fragment> — <record reason>'. The RECORD contradicts these points;
    m0 decided it, m1 obeys and logs them in the sidecar (never silently lost)."""
    out = []
    for ln in stub_sections(stub_text).get("NOT ARGUED", "").splitlines():
        m = re.match(r"^\s*[-*]\s*(.+?)\s+(?:—|--)\s+(.+)$", ln)
        if m: out.append((m.group(1).strip().lower(), m.group(2).strip()))
    return out


def doctrine_rows(path: Path = DOCTRINE) -> dict:
    rows = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("| ") or ln.startswith("| Proposition") or ln.startswith("|---"): continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 2: rows[cells[0]] = cells[1].strip().strip('"').strip("“”")
    return rows


# ---------------------------------------------------------------- voice (comprehension; the exclusion is the guardrail)
_VOICE_SYS = "You classify which party a paragraph of a legal brief argues for. Answer with exactly one word."


def voice_of(text: str, llm_url: str) -> str:
    if not text.strip(): return "EMPTY"
    body = {"model": os.environ.get("M1_SCREEN_MODEL", ""), "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "system", "content": _VOICE_SYS},
                         {"role": "user", "content": f"Paragraph:\n{text[:2500]}\n\nDoes this paragraph argue for the APPELLANT "
                          "(the district court's order was wrong), the APPELLEE (the order was right, the claim fails, the "
                          "judgment should stand), or MIXED? A paragraph that quotes or describes the district court's ruling in order to "
                          "show that ruling was wrong argues for the APPELLANT. One word: APPELLANT, APPELLEE or MIXED."}]}
    try:
        if not body["model"]:
            with urllib.request.urlopen(llm_url.rstrip("/") + "/models", timeout=15) as r:
                body["model"] = json.load(r)["data"][0]["id"]
        req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = json.load(r)["choices"][0]["message"]["content"] or ""
    except Exception as e:
        return f"ERROR:{str(e)[:60]}"
    raw = re.sub(r"(?s)<think>.*?</think>", "", raw).upper()
    for v in ("APPELLEE", "MIXED", "APPELLANT"):
        if v in raw: return v
    return "UNKNOWN"


# ---------------------------------------------------------------- Part II drafter (affirmative: OUR issues, argued FOR us)
# m1_rebuttal is the engine for ANSWERING an opponent: it re-titles every heading "The objection as to X is not well
# taken" and argues against whatever the heading names. A screening argument has no opponent paper — the headings ARE
# Appellant's issues — so routing it through m1_rebuttal argued the appellee's side (wrong instrument, no error anywhere).
# This drafter writes each issue section by section (bounded LLM calls) from m2's own analysis: the issue's argument of
# record, the order passage it answers, and ONLY the authority quotes m2 verified verbatim. The deterministic guard then
# strips any sentence carrying a quotation, citation or docket number that is not in that feed.

def clean_heading(h: str) -> str:
    h = re.sub(r"\s+", " ", h or "").strip()
    h = re.sub(r"\[\d+\]|[\u00b9\u00b2\u00b3\u2070-\u2079]+", "", h)                 # footnote marks
    h = re.sub(r"\s*\((?:\"|“)[^\"”]{1,40}(?:\"|”)\)", "", h)                          # ("Furnisher")
    h = re.split(r"\s*(?:\*?De novo\.?\*?|Preservation:|Standard of review:|\uf0b7)", h, maxsplit=1)[0]
    h = h.strip(" .;:—-")
    if re.search(r"[A-Za-z]-$", (h or "").strip()) or re.search(r"\b\w+’s \w+-?$", h):   # truncated dangle
        h = h.rsplit(" — ", 1)[0] if " — " in h else h
    return h.strip(" .;:—-")


def parse_issues(issues_md: str) -> list[dict]:
    out = []
    for blk in re.split(r"(?m)^(?=\*\*\d+\.\s)", issues_md)[1:]:
        m = re.match(r"\*\*(\d+)\.\s*(.+?)\*\*", blk, re.S)
        if not m: continue
        head = clean_heading(m.group(2))
        rest = blk[m.end():]
        order = re.search(r"(?s)(ECF \d+[^\n]*?(?:says|does not address)[^\n]*)", rest)
        arg = re.search(r"(?s)Appellant's argument of record:\s*(.+)", rest)
        facts = re.search(r"(?s)Record facts:\s*(.+?)(?=\n\s*\n|Appellant's argument of record:|$)", rest)
        out.append({"n": int(m.group(1)), "heading": head, "order": (order.group(1).strip() if order else ""),
                    "argument": re.sub(r"\s+", " ", arg.group(1)).strip() if arg else "",
                    "facts": re.sub(r"\s+", " ", facts.group(1)).strip() if facts else ""})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()   # cells may carry int point numbers as title/claim


_AMJUR_SRC = Path("corpus_sources/amjur2d")
_amjur_cache: dict = {}

def amjur_section_of(vol: str, quote: str) -> str | None:
    """Section number of an Am Jur 2d quote from the SOURCE VOLUME's body heading ('§ 201. Court of different rank.'), never
    from a page running header ('§ 202 COURTS 20 Am Jur 2d' — right-hand pages carry the LAST section on the page, so a chunk
    can open with the next section's header while the quote still belongs to the prior section). None when the volume file
    is not on disk or the quote is not found (then the cite is left as the bot wrote it and logged 'unchecked')."""
    key = vol.upper()
    if key not in _amjur_cache:
        fs = sorted(_AMJUR_SRC.glob(f"vol{key}/*full*.txt"))
        _amjur_cache[key] = fs[0].read_text(encoding="utf-8", errors="replace") if fs else ""
    n = _amjur_cache[key]
    if not n:
        return None
    words = [w for w in re.sub(r"[^A-Za-z ]+", " ", quote).split() if len(w) > 1][:20]
    # the source volume is OCR (footnote marks, '1s' for 'is'): match 6-word windows letting any non-letter junk sit between
    # words, and accept only when every matching window lands under the SAME body heading (else ambiguous -> None)
    heads_hit = set()
    for i in range(0, max(1, len(words) - 5)):
        win = words[i:i + 6]
        if len(win) < 6:
            break
        ms = list(re.finditer(r"[^A-Za-z]+".join(re.escape(w) for w in win), n, flags=re.I))
        if len(ms) == 1:
            # headings: '§ 201. Court of different rank.' and sub-headings '§ 229. — Construction of acts of Congress.'
            hs = re.findall(r"(?m)^\s*§\s*(\d{1,4})\.\s*(?:[—–-]+\s*)?[A-Z]", n[:ms[0].start()])
            if hs:
                heads_hit.add(hs[-1])
    return heads_hit.pop() if len(heads_hit) == 1 else None


def _rank(cite: str) -> int:
    """Rule-selection order for the Rule sentence: enacted text and the rules themselves first, then the Supreme Court,
    then circuit reporters, then treatises (Am Jur states the rule; the enacted text IS the rule)."""
    c = cite or ""
    if re.search(r"U\.S\.C\.|C\.F\.R\.|Fed\. R\. (Civ|App|Crim|Evid)\. P\.|Pa\.R\.C\.P|Const\.", c): return 0
    if re.search(r"\d+ U\.S\. \d+|S\. ?Ct\.", c): return 1
    if re.search(r"F\.\d?d|F\. Supp|F\.4th|A\.\d?d", c): return 2
    return 3


def feed_for(issue: dict, cells: list[dict], max_auth: int = 6) -> list[dict]:
    """Verified authorities from GROUNDED analysis cells on THIS issue (claim number == issue number, or same title)."""
    key = _norm(issue["heading"])[:60]
    seen, out = set(), []
    for c in cells:
        if c.get("group") == "orders" or c.get("status") != "GROUNDED": continue
        if c.get("claim") != issue["n"] and (not key or _norm(c.get("title"))[:60] != key): continue
        for a in c.get("authorities") or []:
            if not a.get("verified") or not (a.get("quote") or "").strip(): continue
            k = (a.get("cite") or "").strip()
            if k in seen: continue
            # a quote the corpus does not attribute to the cited authority is never fed to the drafter (a hybrid of old and
            # restyled Rule 12(d) wording came through a bot cell; the gate caught it after the render — catch it here)
            if re.search(r"U\.S\.C\.|Fed\. R\. (Civ|App|Crim|Evid)\. P\.|Am\. ?Jur\. ?2d", k) and len((a.get("quote") or "").split()) >= 5:
                from pipeline.record_addendum import attributed
                ok = attributed(k, a["quote"])
                if ok is not True:   # False = found only under another authority; None = found nowhere — the gate fails both
                    why = "found only in another authority's text" if ok is False else "NOT IN CORPUS"
                    print(f"  [screen-feed] DROPPED {k[:60]}: {why}: \"{a['quote'][:70]}\"")
                    continue
            m = re.match(r"\s*(\d+[AB]?)\s+Am\. ?Jur\. ?2d\b.*?§\s*(\d{1,4})", k)
            if m:   # section number must be the body heading the quote sits under (page running headers mislead)
                real = amjur_section_of(m.group(1), a["quote"])
                if real is None:
                    print(f"  [screen-feed] {k[:50]}: section unchecked (volume text not on disk / quote not found)")
                elif real != m.group(2):
                    print(f"  [screen-feed] {k[:50]}: § {m.group(2)} -> § {real} (body heading in source volume)")
                    k = k.replace(f"§ {m.group(2)}", f"§ {real}", 1) if f"§ {m.group(2)}" in k else re.sub(r"§\s*\d{1,4}", f"§ {real}", k, count=1)
            seen.add(k); out.append({"cite": k, "quote": a["quote"].strip(), "level": c.get("level")})
    out.sort(key=lambda a: (_rank(a["cite"]), -(a.get("level") if isinstance(a.get("level"), (int, float)) else 0)))   # enacted first; room cells carry level="record"
    return out[:max_auth]


def _chat(llm_url: str, system: str, user: str, max_tokens: int = 1400) -> str:
    # drafting a bounded section is transcription from a supplied feed, not open reasoning: thinking OFF, otherwise
    # the reasoning trace consumes max_tokens and the body comes back empty (v3: 11 of 16 issues returned 0 words)
    body = {"model": os.environ.get("M1_SCREEN_MODEL", ""), "temperature": 0.2, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if not body["model"]:
        with urllib.request.urlopen(llm_url.rstrip("/") + "/models", timeout=15) as r:
            body["model"] = json.load(r)["data"][0]["id"]
    req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = json.load(r)["choices"][0]["message"]["content"] or ""
    raw = re.sub(r"(?s)<think>.*?</think>", "", raw)
    return re.sub(r"(?s)^.*?<think>.*$", "", raw).strip() if "<think>" in raw else raw.strip()


_DRAFT_SYS = ("You write one numbered point of an APPELLANT's argument to a federal court of appeals that is screening "
              "the appeal for frivolousness. You argue FOR the appellant: the order was wrong. Use only the record "
              "facts and the verbatim authority quotations supplied. Quote authority only word for word from the "
              "supplied quotations, followed by its citation exactly as supplied. Never say what an authority "
              "establishes, requires or holds except by quoting it. Never invent a case, statute, "
              "docket number, date or fact. A count of days is stated only with both dates it is computed from in the "
              "same sentence. Plain declarative sentences; no headings, no bullet lists, no hedging.")


_ABBR = re.compile(r"(?:\b(?:v|Co|Inc|Corp|Ltd|No|Nos|Cir|Fed|Civ|App|Bd|Educ|Dist|Ct|Pa|Supp|Ins|Mut|Auto|St|Univ|Ass'n|Int'l|Ex|Exs|Dkt|Dkts|Mem|Op|"
                   r"Dep't|Env't|Gov't|Mr|Ms|Dr|Jr|Sr|Stat|Cong|Sess|Rev|Ann|Cons|Ed|U\.S|U\.S\.C|C\.F\.R|L\.A\.R|I\.O\.P|"
                   r"R|P|e\.g|i\.e|cf|al|id|Id|"
                   # Bluebook party abbreviations (T6) — 'Investors Mortg. | Ins. Co.' was split mid-cite (RUN 21)
                   r"Mortg|Sav|Nat'l|Bancorp|Assocs|Indus|Mfg|Tr|Trs|Ry|R\.R|Elec|Transp|Pharm|Tech|Sys|Serv|Servs|Bros|Hosp|Med|Ctr|"
                   r"Comm'n|Auth|Enters|Grp|Mgmt|Fin|Cas|Sec|Bhd|Soc'y|Inv|Invs|Mkts|Prods|Res|Bldg|Constr|Consol|Util|Litig|"
                   r"Comm|Dev|P'ship|Tel|Mfrs|Admin|Agric|Am|Bus|Chem|Cnty|Coll|Cmty|Equip|Gen|Guar|Hous|Lab|Liab|Mach|Mun|"
                   r"Pac|Pub|Rels|Sch|Sur|Twp|Vill|Mkt|Mtg|Acad|Accts|Adver|Aff|Ass|Atl|Ave|Bd|Cent|Ch|Chi|Chem|Cir|Cmty|Cnty|"
                   r"Cons|Coop|Corr|Del|Det|Dir|Disc|Distrib|E|N|S|W|Fed'n|Found|Hon|Hwy|Imp|Inst|Ins|Int'l|Mech|Merch|Metro|"
                   r"Mktg|Nat|Ne|Nw|Org|Pers|Phila|Pres|Prob|Prof'l|Ref|Reg'l|Reprod|Rest|Ret|Rr|Se|Sw|Steamship|Subcomm|Surety|"
                   r"Tele|Temp|Trans|Tpk|Tr|Trib|Trust|Unif|Vt|Wash|Mo|Ala|Ariz|Ark|Cal|Colo|Conn|Fla|Ga|Haw|Idaho|Ill|Ind|Kan|"
                   r"Ky|La|Me|Md|Mass|Mich|Minn|Miss|Mont|Neb|Nev|N\.H|N\.J|N\.M|N\.Y|N\.C|N\.D|Ohio|Okla|Or|R\.I|S\.C|S\.D|Tenn|Tex|"
                   r"Utah|Va|W\.Va|Wis|Wyo|D\.C)|\b[A-Z])\.$")


def _record_rule(feed, words: int = 40) -> str:
    """Deterministic Rule sentence from the issue's VERIFIED authority feed when the drafter's Rule sentence did not survive
    the guard: the first verified quote carrying mandatory words (shall/must/may not/void), cut to that clause."""
    for a in feed:
        q = re.sub(r"\s+", " ", a.get("quote") or "")
        m = re.search(r"\b(shall|must|may not|cannot|is void|are void)\b", q, re.I)
        if not m:
            continue
        clause = max((c for c in re.split(r"(?<=[.;])\s+", q) if m.group(0) in c), key=len, default=q)
        if len(clause.split()) > words:
            # cut on clause boundaries (commas/semicolons), never mid-phrase: the piece holding the mandatory word, then its
            # neighbours while the cap allows
            parts = [x.strip() for x in re.split(r",|;", clause) if x.strip()]
            k = next((i for i, x in enumerate(parts) if re.search(re.escape(m.group(0)), x, re.I)), 0)
            lo, hi = k, k
            while True:
                grown = False
                if lo > 0 and len(", ".join(parts[lo - 1:hi + 1]).split()) <= words: lo -= 1; grown = True
                if hi < len(parts) - 1 and len(", ".join(parts[lo:hi + 2]).split()) <= words: hi += 1; grown = True
                if not grown: break
            clause = ", ".join(parts[lo:hi + 1]).rstrip(",; ")   # a clause cut never ends on a dangling comma
        return f'Rule: "{clause.strip(" ,;")}" {a["cite"]}.'
    return ""


def _record_violation(it, words: int = 40) -> str:
    """Deterministic Violation sentence from the issue's VERIFIED record facts (verified_record.md) — the guardrail when the
    drafter omits it or the page cap would cut it. Record facts are already verified; nothing is composed."""
    facts = (it.get("facts") or "").strip()
    if not facts:
        return ""
    ss = _sentences(facts)
    keep, n = [], 0
    for x in ss:
        if keep and n + len(x.split()) > words:
            break
        keep.append(x); n += len(x.split())
    out = " ".join(keep).strip()
    return ("Violation: " + out) if out else ""


def _sentences(text: str) -> list[str]:
    """Sentence split that does not break a citation ('Smith v. Jones', 'Fed. R. Civ. P. 12', '3d Cir. 1999')."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])|(?<=[.!?][\"”)])\s+(?=[A-Z“\"(])", text.replace("\n", " ")):
        if not part: continue
        # a part that is itself a bare abbreviation ('Ins.') or opens with one ('Co., 498 U.S.') continues the cite
        if out and (_ABBR.search(out[-1]) or re.match(r"[A-Z][a-z]{1,6}\.(?:,?\s|$)", part)):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


_SELF_LABEL = re.compile(r"(?i)\bpro\s*se\b|\bpro\s*per\b|\bself-represented\b")


def _guard(text: str, issue: dict, feed: list[dict]) -> tuple[str, list[str]]:
    """Drop any sentence whose quotation / citation / docket number is not in the supplied feed."""
    # quotations: m2-verified authority text or the order's own words; citations: m2-verified authorities only (the
    # as-mailed argument paraphrases authority — it is the record of what was argued, not a verified source)
    allowed_q = _norm(" ".join(a["quote"] for a in feed) + " " + issue["order"] + " " + issue.get("facts", ""))
    allowed_c = _norm(" ".join(a["cite"] for a in feed) + " " + issue["heading"] + " " + issue["order"])
    _dsrc = issue["order"] + " " + issue["argument"] + " " + issue["heading"] + " " + issue.get("facts", "")
    allowed_dates = {d for d in (_date(x) for x in _DATE.findall(_dsrc)) if d}
    record_pairs: dict[int, set] = {}
    for _n, _ds in _ecf_date_pairs(_dsrc):
        _dd = _date(_ds)
        if _dd: record_pairs.setdefault(_n, set()).add(_dd)
    # a clause with ONE date gives that date to every entry it names ("ECF 121 and ECF 122 on September 22, 2025" is
    # both entries' date, not the nearest one's) — the same clause rule the record text itself follows
    for _cl in re.split(r"[;.]\s+", _dsrc):
        _cd = [_date(x) for x in _DATE.findall(_cl)]
        _ce = re.findall(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", _cl)
        if len(_cd) == 1 and _cd[0] and 1 <= len(_ce) <= 4:
            for _k in _ce: record_pairs.setdefault(int(_k), set()).add(_cd[0])
    for _a, _b, _ds in re.findall(r"(?:ECF|Dkt\.?)\s*(\d{1,4})\s*[-–]\s*(\d{1,4})[^.;]{0,40}?(" + _DATE.pattern + r")", _dsrc):
        _dd = _date(_ds)
        if _dd and int(_b) - int(_a) <= 6:
            for _k in range(int(_a), int(_b) + 1): record_pairs.setdefault(_k, set()).add(_dd)
    feed_cites = [_REPCITE.search(a["cite"]) for a in feed]
    feed_cites = [m for m in feed_cites if m]
    allowed_d = set(re.findall(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", _dsrc))
    allowed_d |= set(re.findall(r"(?:ECF|Dkt\.?)\s*\d{1,4}\s*[-–]\s*(\d{1,4})", _dsrc))
    for _a, _b in re.findall(r"(?:ECF|Dkt\.?)\s*(\d{1,4})\s*[-–]\s*(\d{1,4})", _dsrc):
        allowed_d |= {str(i) for i in range(int(_a), int(_b) + 1)}
    kept, dropped = [], []
    for s in _sentences(text):
        if not s: continue
        bad = None
        if _SELF_LABEL.search(s):   # filer status is never the rule: the court's duty is (locked: sui juris, never 'pro se')
            bad = "self-label (pro se) — rule must state the court's duty"
        for q in re.findall(r"[“\"]([^”\"]{12,})[”\"]", s):
            if _norm(q) not in allowed_q: bad = f"quote not in feed: {q[:60]}"
        for c in _CITE.findall(s):
            vol = re.search(r"(\d+)\s+(?:U\.S\.|F\.|S\.)|WL (\d+)", c)
            if vol and vol.group(2) and vol.group(2) not in allowed_c: bad = f"cite not in feed: {c[:60]}"; continue
            rep = _REPCITE.search(c)
            if rep:
                # S-05: a reporter cite must match a FEED cite on (volume, reporter, first page) AND — when the drafter
                # wrote them — pin page and year; the volume alone whitelisted '498 U.S. 269, 290 (1999)' against a
                # feed holding '498 U.S. 269, 273 (1991)'
                why = _reporter_mismatch(rep, feed_cites)
                if why: bad = f"cite not in feed: {c[:60]} ({why})"
                continue
            name = _norm(c.split(" v. ")[0])[-20:] if " v. " in c else _norm(c)
            if name and name not in allowed_c and not (vol and vol.group(1) in allowed_c): bad = f"cite not in feed: {c[:60]}"
        for d in re.findall(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", s):
            if d not in allowed_d: bad = f"docket no. not in feed: {d}"
        # S-06: dates are compared as DATES (October 9, 2025 == 10/09/2025) and an (ECF, date) pair in the sentence must
        # be a pair the record carries — a date that belongs to ECF 67 does not license the same date next to ECF 180
        for _n, _ds in _ecf_date_pairs(s):
            _dd = _date(_ds)
            if _dd is None or (_n in record_pairs and _dd not in record_pairs[_n]):
                bad = f"date not in record facts for ECF {_n}: {_ds}"
        _sent_ecfs = set(re.findall(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", s))
        from pipeline.m1_cite_check import _expand_date_lists   # "July 22, August 21, and September 22, 2025" -> each dated
        for dt in _DATE.findall(_expand_date_lists(s)):
            _dd = _date(dt)
            if _dd is None or _dd not in allowed_dates: bad = f"date not in record facts: {dt}"
            # a date the record pairs with specific entries may not be listed beside other entries: "ECF 98 and ECF 103
            # ... mailed ... on July 22" when the record mails ECF 39-41 on July 22 (26-0002 v30 skeptic blocker)
            _owners = {str(n) for n, ds in record_pairs.items() if _dd in ds}
            if _owners and _sent_ecfs and not (_owners & _sent_ecfs):
                bad = f"date {dt} belongs to ECF {', '.join(sorted(_owners, key=int))} in the record, sentence names ECF {', '.join(sorted(_sent_ecfs, key=int))}"
        if not bad:
            for _q in re.findall(r"[“\"]([^”\"]{12,})[”\"]", s):
                _a = next((a for a in feed if _norm(_q) in _norm(a["quote"])), None)
                _short = _norm(re.split(r",| \(| at ", _a["cite"])[0])[:18] if _a else ""
                # the splitter puts a trailing citation in its OWN sentence ('"..." Fed. R. Civ. P. 55(a).'), so test
                # the whole body, not s — testing s re-attached a cite already present (v13: six doubled cites)
                if _a and _short and _short not in _norm(text):
                    s = s.rstrip() + " " + _a["cite"].rstrip(".") + "."
        (dropped if bad else kept).append(bad or s)
    return " ".join(kept).strip(), dropped


_REPCITE = re.compile(r"(\d{1,3})\s+(U\.S\.|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.(?:\s?(?:2d|3d))?|S\.\s?Ct\.)\s+(\d{1,4})(?:,\s*(\d{1,4}))?"
                      r"(?:\s*n\.\s*\d+)?(?:\s*\((?:[\w. ]+?\s)?(\d{4})\))?")


def _reporter_mismatch(m, feed_cites) -> str:
    """'' when (vol, reporter, page) match a feed cite and pin/year, if written, match too; else the reason."""
    vol, rep, page, pin, year = m.group(1), re.sub(r"\s+", " ", m.group(2)), m.group(3), m.group(4), m.group(5)
    same = [f for f in feed_cites if (f.group(1), re.sub(r"\s+", " ", f.group(2)), f.group(3)) == (vol, rep, page)]
    if not same:
        return "volume/reporter/page not in feed"
    if pin and not any(f.group(4) == pin for f in same):
        return f"pin {pin} not in feed ({', '.join(sorted({f.group(4) or '-' for f in same}))})"
    if year and not any(f.group(5) == year for f in same):
        return f"year {year} not in feed ({', '.join(sorted({f.group(5) or '-' for f in same}))})"
    return ""


def _ecf_date_pairs(text: str) -> list[tuple[int, str]]:
    """(ECF, date-string) for each date within 60 chars of an ECF/Dkt number in the same sentence."""
    out = []
    for sent in re.split(r"(?<=[.;])\s+", text.replace("\n", " ")):
        ecfs = [(m.start(), int(m.group(1))) for m in re.finditer(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", sent)]
        for m in _DATE.finditer(sent):
            if not ecfs: break
            pos, n = min(ecfs, key=lambda e: abs(e[0] - m.start()))
            if abs(pos - m.start()) <= 60:
                out.append((n, m.group(0)))
    return out


_DATE = re.compile(r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
                   r"\s+\d{1,2},\s+\d{4}|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
WORDS_PER_PAGE = int(os.environ.get("M1_SCREEN_WPP", "178"))   # MEASURED v10/v11: 742 body words -> 5 pp; 805 -> 6 pp. M1_SCREEN_WPP tightens the budget when a render lands over the court limit (v36 2615: 6 pp at 178)
_UNUSED_WPP = 190          # MEASURED v8: 987 words through the signature rendered 5 pages + 85 words
CAPTION_PAGES = 0.6           # appellate caption block + title (page 1 carried 161 words)
FRAME_WORDS_EST = 280         # COMPACT frame under a limit: Part I ~90 + Part III ~60 + relief ~60 + signature ~50 + heads
MIN_POINT_WORDS = 30          # Rule + Violation: two sentences
CONCISE_POINT_WORDS = int(os.environ.get("M1_SCREEN_POINT_WORDS", "60"))  # user-locked: list the violation, don't over-explain
_RECORD_ANCHOR = re.compile(r"affidavit|certified mail|letters?\b|Common Pleas|exhibit|returned|paid in full", re.I)


def page_limit_of(stub_text: str) -> int | None:
    """Court-ordered page limit as m0 recorded it in the stub ('5-page limit (Clerk's order ...)')."""
    m = re.search(r"(\d{1,2})[- ]page limit", stub_text or "", re.I)
    return int(m.group(1)) if m else None


def select_for_limit(issues: list[dict], cells: list[dict], pages: int, extra_frame: int = 0) -> tuple[list[dict], list[dict], int]:
    """Meet a page limit by COMPRESSING, never by dropping issues: an issue not presented is forfeited, so every issue
    is argued and the per-point word cap is what the page budget leaves after the frame and the point headings.
    (User-locked 2026-10-23: 'picking and choosing means anything we don't bring up we waive'.) Group related errors
    into one issue upstream (m2 OUR_ISSUES) when the count is too high for the page budget."""
    # FRAME_MEASURED: the frame is not a constant — the posture note (premature notice → FRAP 4(a)(2)/FirsTier, ~60 words)
    # exists only for the appeal whose notice preceded judgment (26-0002 landed on page 6 for exactly that reason while
    # 26-0000, same points, fit). The caller passes the measured extra frame words; the per-point cap absorbs them.
    budget = int((pages - CAPTION_PAGES) * WORDS_PER_PAGE) - FRAME_WORDS_EST - int(extra_frame or 0)
    heads = sum(len((it["heading"] or "").split()) + 1 for it in issues)
    n = max(1, len(issues))
    words = max(MIN_POINT_WORDS, min(CONCISE_POINT_WORDS, (budget - heads) // n))
    return list(issues), [], words


def _cap_sentences(body: str, words: int, feed: list[dict]) -> str:
    """Page-limit guardrail (not a rewrite): keep whole sentences up to the cap — the Rule sentence with its verified quote
    and the Violation sentence are never cut; MIRROR / closing sentences go first. Applies to EVERY point body: the m2
    VIOLATION ROW path bypassed it (v38 26-0002: 6 of 8 points were m2 rows, the 3-render loop tightened only the 2
    LLM points, body stayed 1,113-1,141 words against a 506-783 cap, 6 pages three times)."""
    _ss = _sentences(body)
    _qi = next((i for i, x in enumerate(_ss) if re.search(r"[“\"]", x) and any(
        _norm(re.split(r",| \(| at ", a["cite"])[0])[:18] in _norm(x) for a in feed)), None)
    _vi = next((i for i, x in enumerate(_ss) if x.lstrip("*").startswith("Violation:")), None)
    # MIRROR_KEPT (user-locked 2026-10-25): the mirror's closing QUESTION ("§ 455(a): was the rule applied without respect
    # to persons?") is the § 455(a) showing itself — a question for the panel, not a conclusion — and ranks with Rule and
    # Violation; it is never cut. The mirror's hypothetical sentence(s) go first when the budget is short (v39 26-0002:
    # a 40-word cap took all eight mirrors before any was considered).
    _mq = next((i for i, x in enumerate(_ss) if "455(a)" in x and x.rstrip().endswith("?")), None)
    _keep = {i for i in (0, _qi, _vi, _mq) if i is not None}
    _order = [0] + ([_qi] if _qi not in (None, 0) else []) + [i for i in range(1, len(_ss)) if i != _qi]
    _pick, _n = set(), 0
    for i in _order:
        if _pick and _n + len(_ss[i].split()) > words and i not in _keep: continue
        _pick.add(i); _n += len(_ss[i].split())
    return " ".join(_ss[i] for i in sorted(_pick))


def draft_points(issues_md: str, cells: list[dict], llm_url: str, words: int = 110,
                 pages: int | None = None, extra_frame: int = 0) -> tuple[str, list[dict]]:
    """Body in m1's point form ('**n. heading.** text'), one bounded LLM call per issue. With a court page limit,
    issues are selected first (select_for_limit) and the unselected ones are logged as not_argued."""
    issues = parse_issues(issues_md)
    blocks, log = [], []
    if pages:
        issues, _lim_drop, words = select_for_limit(issues, cells, pages, extra_frame=extra_frame)
        print(f"  [page-limit] {pages} pages: arguing {len(issues)} issues at <= {words} words; "
              f"not argued {len(_lim_drop)}: {[d['point'] for d in _lim_drop]}")
        log.extend({"issue": d["point"], "heading": d["title"], "not_argued": d["why"]} for d in _lim_drop)
    for it in issues:
        feed = feed_for(it, cells)
        user = (f"POINT HEADING (keep as is): {it['heading']}\n\n"
                + (f"RECORD FACTS (verified; state them first, with their ECF numbers and dates): {it['facts']}\n\n"
                   if it.get("facts") else "")
                + 
                f"THE ORDER UNDER REVIEW SAYS: {it['order'] or '(the order does not address this issue)'}\n\n"
                f"APPELLANT'S ARGUMENT OF RECORD: {it['argument'][:3500]}\n\n"
                "VERIFIED AUTHORITY QUOTATIONS (the only authority you may quote):\n"
                + ("\n".join(f"- {a['cite']}: “{a['quote'][:600]}”" for a in feed) or "- (none; argue from the record facts above)")
                + f"\n\nWrite EXACTLY TWO sentences, at most {words} words in all. Sentence 1 begins 'Rule:' and quotes "
                  "only the operative mandatory words (shall/must) verbatim from the list, then the citation. Sentence 2 "
                  "begins 'Violation:' and states what the record shows was done instead, with ECF numbers and dates. "
                  "If you state a count of days, write BOTH dates it is computed from in the same sentence "
                  "('served May 10, 2025; removed June 2, 2025 — 23 days'); never state a count whose two dates are "
                  "not on the page. "
                  "No explanation, no characterization, no third sentence: the rule and the record speak. You argue "
                  "ONLY for the appellant; never concede, never adopt the order's reasoning. Output only the two sentences.")
        _ovr = json.load(open(os.environ["M1_SCREEN_POINTS_JSON"])) if os.environ.get("M1_SCREEN_POINTS_JSON") else {}
        # m2 OWNS the content: a GROUNDED 'violation' cell (verified rule quote + record act with ECF) is the point body.
        # m1 only guards it (quotes/cites must be m2-verified; record numbers must appear in the issue's facts) and prints.
        # any GROUNDED cell in the Rule/Violation form (m2 bot "violation" or a relay-room card) keyed to this issue
        _vrow = next((c for c in cells if c.get("status") == "GROUNDED"
                      and (str(c.get("claim")) == str(it["n"]) or _norm(str(c.get("title") or ""))[:60] == _norm(it["heading"])[:60])
                      and any(a.get("verified") for a in c.get("authorities") or [])
                      and re.search(r"(?i)\bRule:.+\bViolation:", c.get("proposition") or "")), None)
        if _vrow and str(it["n"]) not in _ovr:
            # CLASS GUARD A: the relay-room proposal form carries '[corpus:<chunk_id>]' after the cite (legitimate
            # provenance of the cell); it is stripped here, before the guard, and kept in the log row (v34 II.1 leak)
            from pipeline.m1_filing_gate import strip_provenance_tags as _spt
            _prop, _prov = _spt(_vrow["proposition"].strip())
            body, dropped = _guard(_prop, it, feed)
            body = re.sub(r"(?i)\bViol\w*ation\s*:", "Violation:", body)
            body = re.sub(r"^\s*\*?Rule:\*?\s*", "*Rule:* ", body)
            body = re.sub(r"\s\*?Violation:\*?\s*", " *Violation:* ", body, count=1)
            if re.search(r"\*Rule:\*.+\*Violation:\*", body):
                if pages and len(body.split()) > words:
                    body = _cap_sentences(body, words, feed)
                print(f"  [screen-draft] {it['n']:>2} m2 VIOLATION ROW words={len(body.split())} dropped={dropped}")
                log.append({"issue": it["n"], "heading": it["heading"], "authorities": [a["cite"] for a in feed],
                            "used": [a.get("cite") for a in _vrow.get("authorities") or [] if a.get("verified")],
                            "guard_dropped": dropped, "words": len(body.split()), "voice": "M2_VIOLATION",
                            "provenance": [t["tag"] for t in _prov]})
                blocks.append(f"**{it['n']}. {it['heading']}.** " + body)
                continue
        if str(it["n"]) in _ovr:
            body, dropped = _guard(_ovr[str(it["n"])], it, feed)
            print(f"  [screen-draft] {it['n']:>2} FINALIZED body (guarded) words={len(body.split())} dropped={dropped}")
            log.append({"issue": it["n"], "heading": it["heading"], "authorities": [a["cite"] for a in feed],
                        "used": [a["cite"] for a in feed if _norm(a["quote"])[:40] in _norm(body) or
                                 _norm(re.split(r",| \(| at ", a["cite"])[0])[:18] in _norm(body)],
                        "guard_dropped": dropped, "words": len(body.split()), "voice": "FINALIZED"})
            blocks.append(f"**{it['n']}. {it['heading']}.** " + (body or "[NOVEL — no grounded body survived the guard]"))
            continue
        try:
            txt = _chat(llm_url, _DRAFT_SYS, user)
        except Exception as e:
            raise RuntimeError(f"screen drafter could not reach {llm_url} on issue {it['n']}: {e}")
        if not txt.strip():   # one retry with headroom; an empty body is a transport failure, not a drafting result
            txt = _chat(llm_url, _DRAFT_SYS, user, max_tokens=3000)
        txt = re.sub(r"^\*\*.*?\*\*\s*", "", txt).strip()
        _voice = voice_of(txt, llm_url)
        for _try in range(2):
            if _voice not in ("APPELLEE", "MIXED"): break
            txt = _chat(llm_url, _DRAFT_SYS, user + "\n\nYour previous draft conceded the order was right. Rewrite it "
                        "arguing only that the order was WRONG, from the appellant's record facts above.")
            txt = re.sub(r"^\*\*.*?\*\*\s*", "", txt).strip()
            _voice = voice_of(txt, llm_url)
        body, dropped = _guard(txt, it, feed)
        body = re.sub(r"(?i)\bViol\w*ation\s*:", "Violation:", body)      # drafter misspellings of the label
        if not re.search(r"\bRule:", body):
            _rr = _record_rule(feed)
            if _rr:
                body = (_rr + " " + body.lstrip()).strip()
                print(f"  [screen-draft] {it['n']:>2} Rule supplied from verified authority feed")
        if not re.search(r"\bViolation:", body):
            _rv = _record_violation(it)
            if _rv:
                body = (body.rstrip() + " " + _rv).strip()
                print(f"  [screen-draft] {it['n']:>2} Violation supplied from verified record facts")
        if pages and len(body.split()) > words:
            body = _cap_sentences(body, words, feed)
        body = re.sub(r"^\s*\*?Rule:\*?\s*", "*Rule:* ", body)
        body = re.sub(r"\s\*?Violation:\*?\s*", " *Violation:* ", body, count=1)
        _used = [a["cite"] for a in feed if _norm(re.split(r" \(| at ", a["cite"])[0])[:22] in _norm(body)
                 or _norm(a["quote"])[:60] in _norm(body)]
        log.append({"issue": it["n"], "heading": it["heading"], "authorities": [a["cite"] for a in feed], "used": _used,
                    "guard_dropped": dropped, "words": len(body.split()), "voice": _voice})
        print(f"  [screen-draft] {it['n']:>2} auth={len(feed)} words={len(body.split())} dropped={len(dropped)} — {it['heading'][:70]}")
        blocks.append(f"**{it['n']}. {it['heading']}.** " + (body or "[NOVEL — no grounded body survived the guard]"))
    return "\n\n".join(blocks) + "\n", log



def split_points(body_md: str) -> tuple[str, list[tuple[int, str, str]]]:
    """(preamble, [(n, heading, block)]) — m1_rebuttal emits each point as '**n. heading.**'."""
    body_md = re.sub(r"<!--.*?-->\s*", "", body_md, flags=re.S)
    parts = re.split(r"(?m)^(?=\*\*\d+\.\s)", body_md)
    pts = []
    for p in parts[1:]:
        m = re.match(r"\*\*(\d+)\.\s*(.+?)\*\*", p)
        pts.append((int(m.group(1)) if m else 0, (m.group(2) if m else p[:80]).strip().rstrip("."), p.strip()))
    return parts[0].strip(), pts


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def _date(s: str):
    import datetime as _dt
    for f in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s.strip().replace(".", ""), f).date()
        except Exception:
            pass
    return None


def _fmt(d) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def notice_date_of(stub_text: str, appno: str):
    """m0's POSTURE ('notice of appeal ... <date>') first; else the court of appeals' own docket ('Date NOA Filed')."""
    m = re.search(rf"notice of appeal[^.\n]*?((?:{_MONTHS}) \d{{1,2}}, \d{{4}}|\d{{2}}/\d{{2}}/\d{{4}})",
                  stub_sections(stub_text).get("POSTURE", ""), re.I)
    if m:
        return _date(m.group(1))
    if appno and "-" in appno:
        a, b = appno.split("-", 1)
        for f in sorted((ROOT / "corpus_sources" / "ca3_dockets").glob(f"ca3_docket_{a}_{b}_*.txt")):
            m = re.search(r"Date NOA Filed:\s*([A-Z][a-z]+\.? \d{1,2}, \d{4})", f.read_text(errors="replace"))
            if m:
                return _date(m.group(1))
    return None


def judgment_date_of(final_dkt: str, case_dir):
    n = re.search(r"\d+", final_dkt or "")
    f = Path(case_dir) / "CASE_FACTS.md" if case_dir else None
    if not n or not f or not f.exists():
        return None
    m = re.search(rf"(?:Dkt|ECF)\.?\s*(?:No\.?\s*)?{n.group(0)}\b[^\n]{{0,40}}?(\d{{2}}/\d{{2}}/\d{{4}})", f.read_text(errors="replace"))
    return _date(m.group(1)) if m else None


def sibling_appeals(stub_path, appno: str) -> list[str]:
    """Other screen stubs m0 wrote in the same bundle = other live appeals of the same district judgment."""
    p = Path(stub_path).resolve()
    out = []
    for s in sorted(p.parents[1].glob("*/*.md")):
        t = s.read_text(encoding="utf-8", errors="replace")
        if not t.startswith("# STUB") or not _SCREEN_SIGNS.search(stub_sections(t).get("POSTURE", "")):
            continue
        _c, a = caption_of(t)
        if a and a != appno and a not in out:
            out.append(a)
    return out


def posture_note(stub_text: str, stub_path, final_dkt: str) -> str:
    """Jurisdictional posture the panel checks first, derived from the record — never hand-written per appeal.
    Quotes verified: FRAP 4(a)(2) [corpus:507bv#a7423581a5]; FirsTier [corpus:cap_us_498_269_firstier_mortgage_co_v_
    investors_mortgag#20e96468d8]; FRAP 3(b)(2) [corpus:523bv#cd57fd0601]."""
    _c, appno = caption_of(stub_text)
    out = []
    nd = notice_date_of(stub_text, appno)
    case_dir = next((q for q in Path(stub_path).resolve().parents if (q / "CASE_FACTS.md").exists()), None)
    jd = judgment_date_of(final_dkt, case_dir)
    if nd and jd and nd < jd:
        out.append(f"The notice of appeal, filed {_fmt(nd)}, preceded the final judgment ({final_dkt}, {_fmt(jd)}); such a "
                   "notice \"is treated as filed on the date of and after the entry,\" Fed. R. App. P. 4(a)(2), because "
                   "\"certain premature notices do not prejudice the appellee.\" FirsTier Mortg. Co. v. Investors Mortg. "
                   "Ins. Co., 498 U.S. 269, 273 (1991).")
    sib = sibling_appeals(stub_path, appno)
    if sib:
        out.append(f"No. {', '.join(sib)} brings up the same judgment; Appellant asks that the appeals be consolidated. "
                   "See Fed. R. App. P. 3(b)(2).")
    # The whole case is up: every order entered below, derived from the docket (record_addendum.all_orders), listed in the
    # Addendum; the body says so in one sentence so no order can be called outside the notice.
    try:
        from pipeline import record_addendum as _ra
        od = _ra.all_orders(case_dir, Path(stub_path).parent.parent.parent) if case_dir else []
        fd = re.search(r"\d+", final_dkt or "")
        od = [o for o in od if not fd or o[0] <= int(fd.group(0))]   # merger reaches orders up to the judgment itself
        if od:
            out.insert(0, f"Every order entered below — {len(od)} in all, ECF {od[0][0]} through ECF {od[-1][0]}, each listed in "
                          "the Addendum — is before the Court, and Appellant challenges each of them.")
    except Exception as e:
        print(f"  [screen] orders sentence skipped: {e}")
    return " ".join(out)


_SHORT = re.compile(r"\b([A-Z][\w'’]+), (\d+) U\.S\. at (\d+(?:-\d+)?)")

def full_cite_first(text: str, cells: list[dict] | None = None) -> str:
    """Bluebook class rule: the FIRST reference to a case in the paper is the full citation; 'Neitzke, 490 U.S. at 328' may
    appear only after 'Neitzke v. Williams, 490 U.S. 319 (1989)'. The page cap can drop the sentence that carried the full
    form, so this runs on the finished body: for each short form whose case has no earlier full cite, expand it from the
    full forms known to the paper (the doctrine table + the verified cells); unknown cases are left for the gate to flag."""
    fulls: dict = {}
    pool = " ".join(_TEST_CITES.values()) + " " + " ".join((a.get("cite") or "") for c in (cells or []) for a in (c.get("authorities") or []))
    for m in re.finditer(r"([A-Z][\w'’]+)(?: [\w'’.&-]+)* v\. [^,]{2,60}, (\d+) U\.S\. (\d+)(?:, \d+)? \((\d{4})\)", pool):
        fulls.setdefault((m.group(1), m.group(2)), (m.group(0).split(f", {m.group(2)} U.S.")[0], m.group(3), m.group(4)))
    out, pos, done = [], 0, set()
    for m in _SHORT.finditer(text):
        name, vol, pin = m.group(1), m.group(2), m.group(3)
        if (name, vol) in done or re.search(rf"{re.escape(name)}(?: [\w'’.&-]+)* v\. [^,]{{2,60}}, {vol} U\.S\. \d+", text[:m.start()]):
            continue                                   # a full cite already precedes this short form
        done.add((name, vol))
        f = fulls.get((name, vol))
        if not f:
            continue                                   # no full form known — gate reports it
        caption, first, year = f
        out.append(text[pos:m.start()] + f"{caption}, {vol} U.S. {first}, {pin} ({year})"); pos = m.end()
    return "".join(out) + text[pos:]


def frame(body_md: str, final_dkt: str, llm_url: str, drops=None, used=None, note: str = "") -> tuple[str, dict]:
    d = doctrine_rows(); q = lambda k: f"“{d[k]}” {_TEST_CITES[k]}."
    _pre, pts = split_points(body_md)
    kept, skipped = [], []
    for n, head, block in pts:
        _hit = next((r for f, r in (drops or []) if f in head.lower()), None)
        if _hit:
            skipped.append({"point": n, "title": head, "why": f"record: {_hit}"}); continue
        if "[NOVEL" in block:
            skipped.append({"point": n, "title": head, "why": "m1 could not draft (NOVEL — no bank entry)"}); continue
        v = voice_of(re.sub(r"^\*\*.*?\*\*", "", block, count=1), llm_url)
        if v.startswith("ERROR"):   # fail closed: an unchecked point is not shipped as if checked
            raise RuntimeError(f"screen voice check could not run on point {n} ({v}); lane {llm_url}")
        if v in ("APPELLEE", "MIXED") and os.environ.get("M1_SCREEN_PAGE_LIMIT"):
            print(f"  [screen] VOICE-FLAG point {n} ({v}) kept — an omitted issue is waived; skeptic must review")
        elif v in ("APPELLEE", "MIXED"):
            skipped.append({"point": n, "title": head, "why": f"voice: {v}"}); continue
        kept.append((head, block, v))
    renum = [re.sub(r"^\*\*\d+\.", f"**{i}.", b, count=1) for i, (_h, b, _v) in enumerate(kept, 1)]
    if os.environ.get("M1_SCREEN_PAGE_LIMIT"):   # compact frame: the page budget belongs to the issues
        part1 = ("## I. THE TEST THE PANEL APPLIES\n\n"
                 f"An appeal is frivolous only where it lacks an arguable basis in law or in fact. {q('Losing ≠ frivolous')} "
                 "One issue with an arguable basis is enough. "
                 # NON-DISCRETION (user-locked): every rule in Part II is mandatory text; an unanswered point is a duty
                 # not performed. Quote verified [corpus:009ba55c47fbe8cd3ea463959a78aba1].
                 "Each rule below is mandatory, and \"the mandatory 'shall' . . . normally creates an obligation impervious "
                 "to judicial discretion.\" Lexecon Inc. v. Milberg Weiss Bershad Hynes & Lerach, 523 U.S. 26, 35 (1998). "
                 + (f"This appeal is from the final judgment ({final_dkt}), and {MERGER}. "
                    "Every issue is presented below and none is waived.\n" if final_dkt else "Every issue is presented below.\n"))
        if note:
            part1 = part1.rstrip("\n") + " " + note + "\n"
        part2 = ("## II. THE VIOLATIONS: MANDATORY RULE, AND WHAT THE RECORD SHOWS WAS DONE\n\n" + "\n\n".join(renum) + "\n")
        part3 = ("## III. FINDINGS REQUESTED\n\n"
                 "Any disposition under 28 U.S.C. § 1915(e)(2)(B)(i) or L.A.R. 27.4 should state, issue by issue, which "
                 "theory is \"indisputably meritless\" and which fact is \"clearly baseless,\" to permit \"intelligent "
                 "appellate review.\" "
                 f"{_TEST_CITES['What review checks (the court' + chr(39) + 's duties)']}. "
                 # NO-SILENT-DEPARTURE (user-locked 2026-10-25): every point quotes a mandatory text. A panel that does not
                 # apply it must say so in words that can be reviewed — which words of the quoted rule do not mean what they
                 # say, and what authority permits the departure. Silence is the defect the appeal exists to expose.
                 f"As to each of Points 1 through {len(kept)}: if the panel concludes that the rule quoted there does not "
                 "bind the court or was not violated, Appellant asks that the disposition state which words of that rule do not "
                 "mean what they say, and the authority that permits the departure.\n")
        return full_cite_first("\n".join([part1, part2, part3])), {"argued": [h for h, _b, _v in kept], "voice": [v for _h, _b, v in kept],
                                                 "not_argued": skipped, "authorities": []}
    _K_FRIV = 'Meaning of "frivolous"'   # key outside the f-string: backslash in an f-string expression = SyntaxError before 3.12
    part1 = ("## I. THE TEST THE PANEL APPLIES\n\n"
             f"An appeal is frivolous only where it lacks an arguable basis in law or in fact: {q(_K_FRIV)} "
             f"Section 1915 confers {q('Legal frivolousness')[:-1]}. {q('Losing ≠ frivolous')} "
             f"As to facts, {q('Factual frivolousness')[:-1]}. And {q('No factfinding')} "
             "One issue with an arguable basis is enough. "
             + (f"This appeal is from the final judgment ({final_dkt}), and {MERGER}. "
                "Each order and error is argued below.\n" if final_dkt else "Each order under review is argued below.\n"))
    if note:
        part1 = part1.rstrip("\n") + " " + note + "\n"
    part2 = ("## II. EVERY ORDER, EVERY ERROR: EACH HAS AN ARGUABLE BASIS IN LAW AND IN FACT\n\n" + "\n\n".join(renum) + "\n")
    cites = []
    _kept_heads = {_norm(h)[:50] for h, _b, _v in kept}
    for u in (used or []):
        if _norm(u.get("heading"))[:50] in _kept_heads:
            for c in u.get("used") or []:
                if c not in cites: cites.append(c)
    for b in ([] if used is not None else renum):
        for c in _CITE.findall(b):
            c = re.sub(r"\s+", " ", c).strip().rstrip(",.")
            if c not in cites: cites.append(c)
    part3 = ("## III. FINDINGS A FRIVOLOUSNESS DISPOSITION MUST CONTAIN\n\n"
             "The reviewing court examines \"whether the court applied erroneous legal conclusions\" and whether there is \"a "
             "statement explaining the dismissal that facilitates 'intelligent appellate review.'\" "
             f"{_TEST_CITES['What review checks (the court' + chr(39) + 's duties)']}. Appellant therefore asks that any order "
             "dismissing this appeal under 28 U.S.C. § 1915(e)(2)(B)(i), or taking summary action under L.A.R. 27.4 and "
             "I.O.P. 10.6, state for each issue in Part II:\n\n"
             "1. whether the panel holds that the rule, statute, or holding Appellant relies on is an \"indisputably meritless "
             "legal theory\"; and if so, the enacted text or binding holding that says otherwise;\n"
             "2. whether the panel holds the enacted text means something other than what it says, and on what authority;\n"
             "3. which record fact (by Dkt. No. or exhibit) is \"clearly baseless,\" \"fanciful,\" \"fantastic,\" or "
             "\"delusional,\" and by what record it is contradicted.\n"
             + ("\nThe authorities Appellant relies on in Part II are: " + "; ".join(cites) + ".\n" if cites else ""))
    return full_cite_first("\n".join([part1, part2, part3])), {"argued": [h for h, _b, _v in kept], "voice": [v for _h, _b, v in kept],
                                             "not_argued": skipped, "authorities": cites}


RELIEF = ["find that the appeal presents arguable questions of law and fact",
          "decline dismissal under 28 U.S.C. § 1915(e)(2) and summary action under L.A.R. 27.4 and I.O.P. 10.6",
          "under L.A.R. 27.4(b), refer the appeal to a merits panel and direct that briefs be filed"]


def relief_block(name: str) -> str:
    out = ["", "## RELIEF", "", f"WHEREFORE, Appellant {name}, sui juris, respectfully asks the panel to:"]
    out += [f"{i}. {r};" if i < len(RELIEF) else f"{i}. {r}." for i, r in enumerate(RELIEF, 1)]
    return "\n\n".join(out)


def body_words(md: str) -> int:
    """Words that count against a page limit: everything before the certificate of service, minus markdown marks."""
    md = re.split(r"(?mi)^#{1,3}\s*CERTIFICATE OF SERVICE", md)[0]
    return len(re.sub(r"[#*_>|`]", " ", md).split())


def strip_for_limit(md: str) -> str:
    """Under a page limit the Part III authorities roll-call repeats what Part II already cites: omit it."""
    md = re.sub(r"(?m)^The authorities Appellant relies on in Part II are:.*\n?", "", md)
    # selection under a limit: Part II no longer covers "every order, every error"
    md = re.sub(r"(?m)^(## II\.) EVERY ORDER, EVERY ERROR: EACH HAS AN ARGUABLE BASIS IN LAW AND IN FACT",
                r"\1 ISSUES WITH AN ARGUABLE BASIS IN LAW AND IN FACT", md)
    return md.replace(" Each order and error is argued below.", "")


def rendered_pages(docx_path) -> tuple[int, int] | None:
    """(pages through the signature, total pages) of the rendered PDF — the page limit is measured on the paper the
    court receives, not on a word estimate. The certificate of service is outside the limit."""
    from pathlib import Path as _P
    try:
        from generation.finalize_brief import convert_to_pdf
        pdf = _P(str(convert_to_pdf(_P(docx_path))))
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        pages = [(pg.extract_text() or "") for pg in PdfReader(str(pdf)).pages]
    except Exception as e:
        print(f"  [page-limit] render failed ({e}); falling back to word estimate")
        return None
    sig = next((i + 1 for i, t in enumerate(pages) if "/s/" in t), len(pages))
    return sig, len(pages)
