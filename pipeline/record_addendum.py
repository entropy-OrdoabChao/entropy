"""Record addendum for a page-limited appellate paper (CA3 screen argument and its kin).

What it carries, all derived — nothing hand-picked:
  1. INDEX: every district-docket document the argument cites (ECF/Dkt numbers parsed from the filing itself), each
     marked INCLUDED (PDF held in the case intake) or ON THE DISTRICT DOCKET (cited, not held as a PDF — listed, never
     silently dropped), plus every lettered exhibit the argument names.
  2. PERTINENT RULES AND STATUTES (FRAP 28(f) practice): the exact words of each rule/statute the filing quotes, as the
     filing quotes them (every such quote already passed m1's cite/quote guard), plus any verified cell quote for a
     rule/statute the filing cites without quoting.
  3. The held PDFs, in docket order, then the docket sheet, then the exhibits.
Separate document: it does not count against the argument's page limit.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, datetime
from pathlib import Path

_RULE = r"(?:Fed\.\s*R\.\s*(?:Civ|App|Evid)\.\s*P\.\s*\d+[\w()]*|\d+\s+U\.S\.C\.\s*§+\s*[\w\-().]+?(?=[,.;\s]|$)|L\.A\.R\.\s*[\d.()a-z]+)"


def _case_dir(p):
    return next((q for q in Path(p).resolve().parents if (q / "CASE_FACTS.md").exists()), None)


def _body(md: str) -> str:
    return re.split(r"(?mi)^#{1,3}\s*CERTIFICATE OF SERVICE", md)[0]


def cited_dockets(md: str) -> list[int]:
    nums = set()
    for m in re.finditer(r"\b(?:ECF|Dkt\.?)(?:\s*Nos?\.)?\s*((?:\d{1,4}(?:\s*[-–]\s*\d{1,4})?(?:\s*,\s*|\s*;\s*(?:Dkt\.?|ECF)?\s*|\s+and\s+)?)+)",
                         _body(md)):
        for a, b in re.findall(r"(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?", m.group(1)):
            a = int(a); b = int(b) if b else a
            nums.update(range(a, b + 1) if 0 <= b - a <= 10 else [a])
    return sorted(nums)


def cited_exhibits(md: str) -> list[tuple[str, str]]:
    """(letter, the docket entry it rides on or '') for 'Exhibit C' / 'Dkt 134, Exs. D-E'."""
    out = []
    b = _body(md)
    for m in re.finditer(r"\bExhibit\s+([A-Z])\b", b):
        out.append((m.group(1), ""))
    for m in re.finditer(r"\b(?:ECF|Dkt\.?)\s*(?:No\.\s*)?(\d{1,4}),\s*Exs?\.\s*([A-Z])(?:\s*[-–]\s*([A-Z]))?", b):
        lo, hi = m.group(2), m.group(3) or m.group(2)
        out += [(chr(c), m.group(1)) for c in range(ord(lo), ord(hi) + 1)]
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return uniq


def quoted_rules(md: str) -> list[tuple[str, str]]:
    """(cite, exact quoted words) for every rule/statute quote in the filing, in order, de-duplicated."""
    b = _body(md).replace("“", '"').replace("”", '"')
    out = []
    for m in re.finditer(r'"([^"]{8,500}?)[,.]?"[,.]?\s*(?:see\s+)?(' + _RULE + ")", b):
        q, c = m.group(1).strip().rstrip(",."), re.sub(r"\s+", " ", m.group(2)).rstrip(".")
        if (c, q) not in out:
            out.append((c, q))
    return out


_CHUNK_DB = str(_eroot() / "corpus/chunk_text.db")

def _family_ok(cite: str, source: str, text: str) -> bool:
    """Does this corpus chunk belong to the cited authority? USC -> usc_t<T>_s<sec><letter>; Fed. R. -> the rule set's text
    carrying 'Rule <n>'; Am Jur -> amjur2d_<vol>; otherwise accept (reporter cites are checked by the body gate)."""
    src = (source or "").lower()
    m = re.search(r"(\d+)\s+U\.S\.C\.\s*§+\s*(\d+[a-z]?(?:[-–—]\d+)?)", cite)
    if m:
        want = f"usc_t{m.group(1)}_s{re.sub(r'[-–—]', '-', m.group(2).lower())}"
        return re.sub(r"[-–—]", "-", src).startswith(want)
    m = re.search(r"Fed\. R\. (Civ|App|Crim|Evid)\. P\. (\d+)", cite)
    if m:
        fam = {"Civ": "civil", "App": "appellate", "Crim": "criminal", "Evid": "evidence"}[m.group(1)]
        return (fam in src or fam in text.lower()[:400]) and re.search(rf"\bRule {m.group(2)}\b", text) is not None
    m = re.search(r"(\d+[AB]?)\s+Am\. ?Jur\. ?2d", cite)
    if m:
        return src.startswith(f"amjur2d_{m.group(1).lower()}")
    return True


def attributed(cite: str, quote: str) -> bool | None:
    """True when the quoted words sit in a corpus chunk OF THE CITED AUTHORITY; False when they are found only in another
    authority's chunk (a gloss mislabelled as statute text — Neitzke's 'arguable basis' is not § 1915's words); None when
    the corpus cannot be consulted. Read-only FTS, no model."""
    import sqlite3
    words = re.sub(r"\s+", " ", quote).strip().split()
    if len(words) < 4:
        return None
    phrase = " ".join(words[:12]).replace('"', "")
    # ask the corpus directly "is this phrase in a chunk of the cited authority?" — never sample N hits and hope the
    # family is among them (Neitzke's gloss is quoted in hundreds of opinions; a 40-row sample is arbitrary)
    prefix = None
    m = re.search(r"(\d+)\s+U\.S\.C\.\s*§+\s*(\d+[a-z]?(?:[-–—]\d+)?)", cite)
    if m:
        # source keys write the section dash as an EN dash (usc_t15_s1681s–2); cites use a hyphen — match either
        sec = re.sub(r"[-–—]", "_", m.group(2).lower())
        prefix = f"usc_t{m.group(1)}_s{sec}"
    m2 = re.search(r"(\d+[AB]?)\s+Am\. ?Jur\. ?2d", cite)
    if m2:
        prefix = f"amjur2d_{m2.group(1).lower()}"
    try:
        con = sqlite3.connect(f"file:{_CHUNK_DB}?mode=ro", uri=True, timeout=20)
        # chunk_text is WITHOUT ROWID; the FTS table carries the chunk_id as source_id
        if prefix:
            hit = con.execute("select 1 from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id "
                              "where chunk_text_fts match ? and t.source like ? limit 1", (f'"{phrase}"', prefix + "%")).fetchone()   # '_' in LIKE = any one char: covers -, – and —
            anywhere = hit or con.execute("select 1 from chunk_text_fts where chunk_text_fts match ? limit 1", (f'"{phrase}"',)).fetchone()
            if not anywhere:
                # OCR sources (Am Jur scans) break a 12-word phrase with footnote marks glued to words ('mandatory®') and
                # line-end hyphenation ('jurisdic-\ntional'); ask with 6-word windows (step 3) and require two windows in the
                # cited family before saying True — one window of plain English is not attribution
                wins = [" ".join(words[i:i + 6]).replace('"', "") for i in range(0, max(1, len(words) - 5), 3)]
                fam = sum(1 for w in wins if con.execute("select 1 from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id "
                                                         "where chunk_text_fts match ? and t.source like ? limit 1", (f'"{w}"', prefix + "%")).fetchone())
                other = any(con.execute("select 1 from chunk_text_fts where chunk_text_fts match ? limit 1", (f'"{w}"',)).fetchone() for w in wins)
                con.close()
                if fam >= min(2, len(wins)):
                    return True
                return False if other else None   # found only elsewhere -> False; nowhere -> NOT IN CORPUS
            con.close()
            return bool(hit)
        rows = con.execute("select t.source, t.text from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id "
                           "where chunk_text_fts match ? limit 200", (f'"{phrase}"',)).fetchall()
        con.close()
    except Exception as e:
        print(f"  [addendum] attribution unchecked ({e.__class__.__name__}: {e})")
        return None
    if not rows:
        return None
    return any(_family_ok(cite, src, txt) for src, txt in rows)


def cited_rules(md: str) -> list[str]:
    seen = []
    for m in re.finditer(_RULE, _body(md)):
        c = re.sub(r"\s+", " ", m.group(0)).rstrip(".")
        if c not in seen:
            seen.append(c)
    return seen


def _pdf_for(n: int, intake: Path):
    for pat in (f"dkt_{n}_*.pdf", f"dkt_{n}.pdf", f"ecf {n}.pdf", f"ecf_{n}.pdf", f"ECF_{n}_*.pdf", f"ECF {n}.pdf"):
        hits = sorted(h for h in intake.glob(pat) if h.suffix.lower() == ".pdf")
        if hits:
            return hits[0]
    return None


def _docket_sheet(intake: Path):
    hits = sorted((h for h in intake.glob("*.pdf") if re.search(r"docket", h.name, re.I) and "activity" not in h.name.lower()),
                  key=lambda h: h.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _exhibit_pdf(letter: str, case_dir: Path):
    for d in (case_dir / "exhibits", case_dir / "intake"):
        if not d.is_dir():
            continue
        for h in sorted(d.rglob("*.pdf")):
            if re.search(rf"(?i)\bex(?:hibit)?[\s_\-]*{letter}(?![A-Za-z])", h.stem):
                return h
    return None


def _cell_quotes(cells_path) -> dict:
    out = {}
    try:
        for l in Path(cells_path).read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            for a in (json.loads(l).get("authorities") or []):
                if a.get("verified") and a.get("cite") and a.get("quote"):
                    out.setdefault(re.sub(r"\s+", " ", a["cite"]).rstrip("."), a["quote"])
    except Exception:
        pass
    return out


def order_answered(intake: Path, appno: str) -> list[str]:
    """The screening order this paper answers, verbatim from the case's own intake OCR (the clerk's paper service): the
    panel's self-stated test goes into the record beside the argument. Derived — the sidecar is found by appellate number
    + the screening statute; nothing typed by hand. Empty list (and a printed gap) when no scan is in intake."""
    if not appno:
        return []
    for t in sorted((intake / "_ocr").glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            txt = t.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if f"No. {appno}" in txt and "1915(e)(2)" in txt:
            blocks = re.split(r"(?m)^(?=No\. \d{2}-\d{4}\s*$)", txt)
            for b in blocks:
                if b.startswith(f"No. {appno}"):
                    lines = [" ".join(x.split()) for x in b.splitlines() if x.strip()]
                    return [f"[source: intake/_ocr/{t.name}]"] + lines
    print(f"  [addendum] screening order for No. {appno} NOT IN INTAKE (no OCR sidecar with the order text)")
    return []


def issue_rows(md: str) -> list[tuple[int, str, list[int]]]:
    """Part II of the filing, one row per numbered issue: (n, heading, ECF numbers the row relies on)."""
    body = re.search(r"## II\..*?(?=\n## III\.|\Z)", md, re.S)
    rows = []
    for m in re.finditer(r"(?m)^\*\*(\d{1,2})\.\s+(.+?)\*\*(.*)$", body.group(0) if body else ""):
        ecfs = sorted({int(x) for x in re.findall(r"(?:ECF|Dkt\.?(?: No\.)?)\s*(\d{1,3})", m.group(3))})
        rows.append((int(m.group(1)), normalize_title(m.group(2)), ecfs))   # C11 normalizer: one path for every heading
    return rows


def findings_requested(rows) -> list[str]:
    """One requested finding per issue: the panel is asked to state, on the record, whether THIS issue has an arguable
    basis in law or fact and, if not, which quoted rule does not apply and why. A disposition that labels the appeal
    frivolous without answering these rows is itself a record fact for rehearing (FRAP 35, 40) and certiorari."""
    out = []
    for n, h, ecfs in rows:
        rec = (", ".join(f"ECF {e}" for e in ecfs)) if ecfs else "the record cited in the argument"
        out.append(f"Finding {n} — {h}. Record: {rec}. Requested: whether this issue has an arguable basis in law or in fact "
                   f"(28 U.S.C. § 1915(e)(2)(B)(i)); if the panel finds none, which quoted rule does not govern this record, and why.")
    return out or ["(The argument states no numbered issues.)"]


def all_orders(case_dir: Path, bundle_dir: Path | None = None) -> list[tuple[int, str, str]]:
    """Every order/memorandum the district court entered, derived — never a hand list: the bundle's DISTRICT_ORDERS_INDEX.md
    (built from the docket) plus every NEF/docket PDF in intake (dkt_<n>_*.doctext.txt) whose first lines are an ORDER or
    MEMORANDUM. Returns (ecf, date, first ~110 chars of the docket text) sorted by ECF. Under the merger rule every one is
    before the appellate court, so the paper lists them all (addendum) and the body says so in one sentence."""
    out = {}
    for d in filter(None, [bundle_dir, case_dir / "drafts"]):
        for f in Path(d).rglob("DISTRICT_ORDERS_INDEX.md"):
            for m in re.finditer(r"(?m)^- \*\*ECF (\d+)\*\* \((\d\d/\d\d/\d{4})\): (.+)$", f.read_text(encoding="utf-8", errors="replace")):
                out.setdefault(int(m.group(1)), (m.group(2), " ".join(m.group(3).split())[:110]))
    for t in (case_dir / "intake").glob("dkt_*_*.doctext.txt"):
        n = re.match(r"dkt_(\d+)_", t.name)
        if not n or int(n.group(1)) in out:
            continue
        head = t.read_text(encoding="utf-8", errors="replace")[:1500]
        # attachments ('Document 174-2') are exhibits/proposed orders of the main filing, never orders of the court
        if re.search(r"Document \d+-\d+", head[:300]) or re.search(r"_part\d+\.pdf", t.name):
            continue
        # an ORDER of the district court: issued by that court (not a court-of-appeals order docketed below) and the
        # document's own first heading is ORDER/MEMORANDUM (a MOTION carrying a proposed order is a motion)
        first = re.search(r"(?m)^\s*(?:[A-Z][A-Z'’,.\- ]{0,60})?\b(ORDER|MEMORANDUM|MOTION|NOTICE|BRIEF|RESPONSE|REPLY|LETTER|DECLARATION)\b", head)
        if "COURT OF APPEALS" in head.upper()[:600] or not first or first.group(1) not in ("ORDER", "MEMORANDUM"):
            continue
        if re.search(r"(?m)^\s*(ORDER|MEMORANDUM)\s*$", head):
            dm = re.search(r"this (\d+)(?:st|nd|rd|th) day of (\w+), (\d{4})", head)
            date = f"{dm.group(2)} {dm.group(1)}, {dm.group(3)}" if dm else "date in NEF"
            kind = "MEMORANDUM" if re.search(r"(?m)^\s*MEMORANDUM\s*$", head) else "ORDER"
            body = re.search(r"it is (?:hereby )?ORDERED[: ]+(.{0,110})", head, re.S)
            out[int(n.group(1))] = (date, kind + (": " + " ".join(body.group(1).split()) if body else ""))
    return sorted((k, v[0], v[1]) for k, v in out.items())


# ----------------------------------------------------------------------------------------------------------------
# ISSUE COVERAGE (Part V 'ISSUES PRESERVED'): proves nothing is waived. The all-orders matrix is the STANDARD runner
# (`M2_OUT=<units>.m2 python3 -m pipeline.m2.analysis_runner ALL_ORDERS_UNITS.md --bots discretion_closer`, stage 0 of
# run_screen.sh); this reads its cells.jsonl + OUR_ISSUES.md directly. The bot's own answer names the issue numbers
# (comprehension); code only verifies them against the issue list, re-verifies each rule quote through
# card_gate.verify_phrase (the ONE quote verifier) and tabulates. Never a keyword table pairing orders to issues.
_UNIT_RX = re.compile(r"(?m)^\*\*(\d{1,3})\.\s+(Appellant'?s challenge to ECF\s+([\d/ ]+?)\s*\(([^)]*)\)[^*]*)\*\*")
_ISSUE_RX = re.compile(r"(?m)^\*\*(\d{1,3})\.\s+(.+?)\*\*\s*$")
_ECF_RX = re.compile(r"\bECF(?:\s*No\.?)?\s*(\d{1,4})\b", re.I)
_ISSUE_REF = re.compile(r"\bissues?\s*(?:nos?\.?|numbers?|#)?\s*((?:\d{1,2}\s*(?:,|and|&|/|–|-|to)?\s*)+)", re.I)
_STOP = set("the a an of to in on for by and or is was were be been are at as with under from that this its it his her their "
            "court district erred error judgment order orders motion rule fed civ p r v no de novo preservation issue whether".split())
COVERAGE_BOT = "discretion_closer"


def parse_units(md: str) -> list[dict]:
    """ALL_ORDERS_UNITS.md -> [{unit, ecfs, date, title}]; a unit may name two ECFs (`121/122`)."""
    return [{"unit": int(m.group(1)), "ecfs": [int(x) for x in re.findall(r"\d+", m.group(3))],
             "date": (m.group(4).split() or [""])[0], "title": " ".join(m.group(2).split())} for m in _UNIT_RX.finditer(md)]


# --- title normalizer (2026-10-24, SKEPTIC C11: '…("Furnisher")[2]'s Mid-.', 'De novo. Preservation: …' inside the
# title, '60(d) (3)..'). CLASS RULE: every heading parsed from OUR_ISSUES.md (or the filing's Part II) passes through
# normalize_title — footnote markers off, standard-of-review / preservation fragments off, '(d) (3)' -> '(d)(3)',
# '..'/'-.' tails off, and a heading truncated mid-word is cut back to its last complete clause. Deterministic, no LLM.
_SOR_RX = re.compile(r"\s*\*?\b(?:De novo|Abuse of discretion|Plenary(?: review)?|Clear(?:ly)? error(?:oneous)?|Plain error|"
                     r"Mixed question[^.*]*)\.?\*?(?=\s|$)", re.I)
_PRESERVATION_RX = re.compile(r"\s*\*?\bPreservation\s*:.*$", re.I | re.S)
_DANGLING_RX = re.compile(r"(?:\s|^)(?:a|an|the|of|to|in|on|for|by|and|or|from|with|under|its|his|her|their|that|was|is|"
                          r"were|are|be|as|at|whether|because|which|who|’s|'s)\s*$", re.I)


def normalize_title(t: str) -> str:
    """One issue heading -> the clean title Part V prints. Pure function; see the class rule above."""
    import unicodedata
    t = "".join(ch for ch in str(t or "") if unicodedata.category(ch) != "Co" and ch not in "\u2022\u25aa\u25e6\u2023\u00b7")   # Word bullets (U+F0B7), •
    t = " ".join(t.split())
    t = re.sub(r"\[\d{1,3}\]", "", t)                                   # footnote markers [2]
    t = _PRESERVATION_RX.sub("", t)                                      # 'Preservation: …' fragment (always a tail)
    t = _SOR_RX.sub("", t)                                               # 'De novo.' / '*De novo.*' / 'Abuse of discretion.'
    t = re.sub(r"\)\s+\((?=[0-9A-Za-z]{1,4}\))", ")(", t)              # '60(d) (3)' -> '60(d)(3)'
    t = re.sub(r"\s+([,;:.])", r"\1", t)
    t = re.sub(r"\.{2,}", ".", t).strip()
    # truncated mid-word / mid-clause: 'Mid-', '…Appeal from', '…Rule 60(b)(4)' (brief-extraction cut) -> cut back to the
    # last complete clause boundary that still leaves a sentence-like head; else drop the dangling tail word(s)
    truncated = bool(re.search(r"[\w)’'\"]-$", t)) or bool(_DANGLING_RX.search(t)) or t.count("(") > t.count(")")
    if truncated:
        cut = max(t.rfind(" — "), t.rfind(" – "), t.rfind(": "), t.rfind("; "))
        if cut > 0 and len(t[:cut].split()) >= 4:
            t = t[:cut]
        else:
            t = re.sub(r"\s*\S*-$", "", t)                                 # the half word
            for _ in range(3):
                t2 = _DANGLING_RX.sub("", t)
                if t2 == t:
                    break
                t = t2
            while t.count("(") > t.count(")") and "(" in t:
                t = t[:t.rfind("(")].rstrip()
    t = t.rstrip(" -–—,;:.").strip()
    return re.sub(r"\s+", " ", t)


def parse_issues(md: str) -> dict[int, str]:
    """OUR_ISSUES.md -> {n: title} (each title through normalize_title)."""
    return {int(n): normalize_title(t) for n, t in _ISSUE_RX.findall(md)}


# --- chunk-grounded quotes (2026-10-24, SKEPTIC C4 + C5). CLASS RULES: (C4) a quoted rule excerpt must end on a sentence
# boundary taken from the verified chunk text — extend to the sentence end or cut back to the previous one, never a hard
# char cap mid-sentence; (C5) the cited subsection label is derived from the subsection whose text is actually quoted —
# locate the quote inside the chunk, read the enclosing '(a)(5)' markers — never from the cell's label when they disagree.
_CHUNK_CACHE: dict = {}
_ABBR = {"fed", "r", "civ", "app", "crim", "evid", "p", "no", "nos", "u.s.c", "v", "cf", "e.g", "i.e", "id", "sec", "stat", "cir", "ch"}
_FAM_PREFIX = {"Civ": ["frcp_rule_{n}", "federal-rules-of-civil-procedure"], "App": ["frap_rule_{n}", "federal-rules-of-appellate-procedure"],
               "Crim": ["frcrp_rule_{n}", "federal-rules-of-criminal-procedure"], "Evid": ["fre_rule_{n}", "federal-rules-of-evidence"]}


class _Corpus:
    """Read-only chunk_text.db access: text(chunk_id) by primary key; family(cite) -> [(chunk_id, text)] of the cited
    rule's own source family (index range scan on source, never a whole-table LIKE). Tests inject a stand-in with the
    same two methods."""
    def _con(self):
        import sqlite3
        return sqlite3.connect(f"file:{_CHUNK_DB}?mode=ro", uri=True, timeout=20)

    def text(self, chunk_id):
        con = self._con()
        try:
            r = con.execute("select text from chunk_text where chunk_id=?", (chunk_id,)).fetchone()
        finally:
            con.close()
        return (r[0] if r else "") or ""

    def family(self, cite):
        m = re.search(r"Fed\. R\. (Civ|App|Crim|Evid)\. P\. (\d+)", cite)
        pre = [x.format(n=m.group(2)) for x in _FAM_PREFIX[m.group(1)]] if m else []
        m2 = re.search(r"(\d+)\s+U\.S\.C\.\s*§+\s*(\d+[a-z]?(?:[-–—]\d+)?)", cite)
        if m2:
            pre.append(f"usc_t{m2.group(1)}_s{m2.group(2).lower()}")
        rows = []
        if not pre:
            return rows
        con = self._con()
        try:
            for p in pre:
                rows += con.execute("select chunk_id, text from chunk_text where source >= ? and source < ? limit 400", (p, p + "~")).fetchall()
        finally:
            con.close()
        return rows


_CORPUS = None


def _corpus(lookup=None):
    global _CORPUS
    if lookup is not None and hasattr(lookup, "text"):
        return lookup
    if _CORPUS is None:
        _CORPUS = _Corpus()
    return _CORPUS


def chunk_text_of(chunk_id, lookup=None) -> str:
    """Verified chunk's text by primary key (ms lookup, read-only). `lookup` injectable for tests (callable or _Corpus-like)."""
    if not chunk_id:
        return ""
    if chunk_id in _CHUNK_CACHE:
        return _CHUNK_CACHE[chunk_id]
    txt = ""
    try:
        txt = (lookup(chunk_id) if callable(lookup) and not hasattr(lookup, "text") else _corpus(lookup).text(chunk_id)) or ""
    except Exception as e:
        print(f"  [coverage] chunk {chunk_id} unreadable ({e.__class__.__name__})")
    _CHUNK_CACHE[chunk_id] = txt
    return txt


def _norm_map(text: str):
    """Chunk text -> (normalized text, index map norm->orig). Normalization: line-break hyphenation 'rea- sons' joined,
    curly quotes straightened, whitespace collapsed, lower-cased — so a cell quote typed from the rule finds itself."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2014", "-").replace("\u2013", "-")
    out, idx, i, n = [], [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and i + 1 < n and text[i + 1].isspace() and i > 0 and text[i - 1].isalpha():
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j].islower():          # 'rea- sons' -> 'reasons'
                i = j; continue
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" "); idx.append(i)
            i += 1; continue
        out.append(ch.lower()); idx.append(i); i += 1
    idx.append(n)
    return "".join(out), idx


def _norm_q(q: str) -> str:
    return _norm_map(q)[0].strip()


def _is_sentence_end(norm: str, k: int) -> bool:
    """norm[k] closes a sentence: one of . ; : ! ? followed by a space (or the end), and for '.' the word before it is not
    an abbreviation ('Fed.', 'R.', 'U.S.C.') or a single letter."""
    if k >= len(norm) or norm[k] not in ".;:!?":
        return False
    nxt = norm[k + 1:k + 2]
    if nxt and nxt != " ":
        return False
    if norm[k] == ".":
        w = re.search(r"([a-z.§]+)$", norm[:k])
        if w and (w.group(1) in _ABBR or len(w.group(1)) == 1):
            return False
    return True


def _dehyph(text: str) -> str:
    return " ".join(re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", text).split())


def sentence_bounded(quote: str, chunk: str, cap: int = 0) -> tuple[str, str]:
    """(quote ending on a sentence boundary of the chunk, how). Locates the quote in the chunk (normalized), then: if it
    already ends a sentence -> unchanged; else extends to the next sentence end; with `cap` > 0 and the sentence-complete
    text longer than cap -> cuts back to the last sentence end within cap (never below one whole sentence)."""
    q = _norm_q(quote)
    if not q:
        return quote, "empty"
    norm, idx = _norm_map(chunk) if chunk else ("", [])
    s = norm.find(q) if norm else -1
    if s < 0:
        # no chunk / not located: the only safe move is on the quote itself — cut back to its own last sentence end
        return _clip_sentences(quote, cap), "not located in chunk"
    e = s + len(q)
    if _is_sentence_end(norm, e - 1):
        end_pos, how = e - 1, "ends on sentence boundary"
    elif e < len(norm) and _is_sentence_end(norm, e):
        end_pos, how = e, "ends on sentence boundary"
    else:
        k = e
        while k < len(norm) and not _is_sentence_end(norm, k):
            k += 1
        if k >= len(norm):
            return _clip_sentences(quote, cap), "chunk ends mid-sentence"
        end_pos, how = k, "extended to sentence end"
    text = _dehyph(chunk[idx[s]:idx[end_pos] + 1])
    if cap and len(text) > cap:
        inner = [k for k in range(s, end_pos) if _is_sentence_end(norm, k) and (idx[k] + 1 - idx[s]) <= cap]
        if inner:
            text = _dehyph(chunk[idx[s]:idx[inner[-1]] + 1])
            how += "; cut back to sentence end within cap"
        else:
            how += "; first sentence longer than cap, kept whole"
    return text, how


def _clip_sentences(quote: str, cap: int) -> str:
    """Quote with no chunk to consult: never cut mid-sentence. Over cap -> back to the last sentence end within cap;
    none -> the whole quote (a long first sentence beats a truncated one)."""
    q = " ".join(str(quote).split())
    if not cap or len(q) <= cap:
        return q
    cut = max((m.end() for m in re.finditer(r"[.;:!?](?=\s)", q[:cap]) if not re.search(r"\b(?:fed|r|civ|app|p|no|u\.s\.c)\.$", q[:m.end()].lower())), default=0)
    return q[:cut].rstrip() if cut else q


_MARK_RX = re.compile(r"(?:(?<=\s)|(?<=^))\(([a-z]|\d{1,2}|[A-Z]|[ivx]{1,4})\)(?=\s)")
_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def subsection_at(chunk: str, quote: str) -> list[str]:
    """Enclosing subsection markers ['a', '5'] of the point in `chunk` where `quote` starts, read from the chunk's own
    '(a) … (1) … (A) …' structure (letter -> level 1, number -> level 2, capital -> level 3, roman -> level 4). [] when
    the quote is not located or no marker precedes it. Inline references like 'Rule 24(a)(3)' are not markers (no space
    before the paren)."""
    norm, idx = _norm_map(chunk)
    s = norm.find(_norm_q(quote))
    if s < 0:
        return []
    head = chunk[:idx[s]]
    stack: list = []
    for m in _MARK_RX.finditer(head):
        tok = m.group(1)
        if tok.isdigit():
            lvl = 2
        elif tok.isupper():
            lvl = 3
        elif tok in _ROMAN and len(stack) >= 3:
            lvl = 4
        else:
            lvl = 1
        if lvl > len(stack) + 1:            # marker deeper than the known tree (chunk starts mid-section): unknown parents
            stack = stack + [None] * (lvl - 1 - len(stack))
        stack = stack[:lvl - 1] + [tok]
    return stack


_CITE_SUB_RX = re.compile(r"^(.*?\b(?:P\.\s*\d+[a-z]?|§+\s*\d+[a-z]?(?:[-–—]\d+[a-z]?)?))((?:\([0-9A-Za-z]{1,4}\))*)(.*)$")


def relabel_cite(cite: str, chunk: str, quote: str) -> tuple[str, str | None, bool]:
    """(cite whose subsection matches the quoted words, the label it replaced or None, complete). Only the depth the cite
    itself carries is compared ('24(a)(1)' -> two levels); a cite without a subsection is left alone. A level the chunk
    cannot show (chunk starts mid-section -> None) is kept from the cite and `complete` is False — the caller then looks
    for a chunk of the rule's own family that shows the whole tree; a proven disagreement at a known level is still a
    relabel (the quoted words demonstrably sit elsewhere)."""
    m = _CITE_SUB_RX.match(" ".join(cite.split()))
    if not m or not m.group(2):
        return cite, None, True
    want = re.findall(r"\(([0-9A-Za-z]{1,4})\)", m.group(2))
    stack = subsection_at(chunk, quote)
    if not stack or len(stack) < len(want):
        return cite, None, False
    got = [g if g is not None else w for g, w in zip(stack[:len(want)], want)]
    complete = all(x is not None for x in stack[:len(want)])
    if [x.lower() for x in got] == [x.lower() for x in want]:
        return cite, None, complete
    return m.group(1) + "".join(f"({x})" for x in got) + m.group(3), cite, complete


def ground_departure(d: dict, lookup=None) -> dict:
    """Apply C4 + C5 to one verified departure record in place: quote completed to a sentence boundary of its chunk,
    cite relabelled to the subsection the quote sits in (recorded as `relabelled_from`), `grounding` says what was done.
    When the verified chunk cannot show the full subsection tree (a reprint that starts mid-rule), the rule's own
    family chunk that holds the quote is consulted (`grounded_in`)."""
    chunk = chunk_text_of(d.get("chunk_id"), lookup) if d.get("verified") == "[corpus]" else ""
    if not chunk:
        d["quote"], d["grounding"] = _clip_sentences(d.get("quote") or "", 0), "no chunk text"
        return d
    new_cite, old, complete = relabel_cite(d["cite"], chunk, d["quote"])
    if not complete:
        try:
            fam = _corpus(lookup).family(d["cite"]) if not (callable(lookup) and not hasattr(lookup, "text")) else []
        except Exception as e:
            print(f"  [coverage] family lookup failed ({e.__class__.__name__})"); fam = []
        q = _norm_q(d["quote"])
        for cid, txt in fam:
            if q in _norm_map(txt or "")[0]:
                c2, o2, comp2 = relabel_cite(d["cite"], txt, d["quote"])
                if comp2:
                    new_cite, old, chunk, d["grounded_in"] = c2, o2, txt, cid
                    break
    q, how = sentence_bounded(d["quote"], chunk)
    d["quote"], d["grounding"] = q, how
    if old:
        d["cite"], d["relabelled_from"] = new_cite, old
        d["grounding"] += f"; cite relabelled from {old} (quoted words sit in {new_cite})"
    return d


def title_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9'’\-]{3,}", title.lower()) if w not in _STOP}


def issue_numbers_in(text: str, issues: dict) -> tuple[list[int], list[int]]:
    """Issue numbers the bot's OWN answer names (`Issue 2`, `issues 3, 5 and 12`, `Issues 2–4`) ->
    (numbers OUR_ISSUES holds, numbers it does not — dropped and reported, never guessed)."""
    found, bogus = set(), set()
    for m in _ISSUE_REF.finditer(text):
        span = m.group(1)
        for r in re.finditer(r"(\d{1,2})\s*(?:–|-|to)\s*(\d{1,2})", span):
            a, b = int(r.group(1)), int(r.group(2))
            if 0 < a <= b <= 99:
                for n in range(a, b + 1):
                    (found if n in issues else bogus).add(n)
        for n in re.findall(r"\d{1,2}", re.sub(r"\d{1,2}\s*(?:–|-|to)\s*\d{1,2}", " ", span)):
            (found if int(n) in issues else bogus).add(int(n))
    return sorted(found), sorted(bogus)


def issue_titles_in(text: str, issues: dict, k: int = 3) -> list[int]:
    """Issues whose title the answer restates: >=k distinctive title tokens inside one sentence."""
    sents = [set(re.findall(r"[a-z][a-z0-9'’\-]{3,}", s)) for s in re.split(r"(?<=[.;:])\s+|\n", text.lower())]
    return sorted(n for n, t in issues.items() if len(title_tokens(t)) >= k and any(len(title_tokens(t) & s) >= k for s in sents))


def _verify_phrase():
    try:
        import sys as _sys
        # the repo's scripts/rooms (also when this module runs from a _sandbox_* overlay, whose parent has no scripts/)
        rooms = next((q / "scripts" / "rooms" for q in Path(__file__).resolve().parents if (q / "scripts" / "rooms" / "card_gate.py").exists()), None)
        _sys.path.insert(0, str(rooms or Path(__file__).resolve().parents[1] / "scripts" / "rooms"))
        import card_gate
        return card_gate.verify_phrase
    except Exception as e:
        print(f"  [coverage] WARN card_gate unavailable ({e.__class__.__name__}: {e}); quotes marked UNVERIFIED")
        return None


def departures_of(cell: dict, verify, lookup=None) -> list[dict]:
    """The cell's authorities, each quote re-verified by card_gate.verify_phrase -> [corpus] / NOT IN CORPUS, then
    grounded in the verified chunk (ground_departure: sentence-complete quote, subsection read from the chunk)."""
    out = []
    for a in cell.get("authorities") or []:
        cite, quote = str(a.get("cite") or "").strip(), " ".join(str(a.get("quote") or "").split())
        if not cite:
            continue
        if not quote:
            out.append({"cite": cite, "quote": "", "verified": "NOT IN CORPUS", "how": "no quote"}); continue
        if verify is None:
            out.append({"cite": cite, "quote": quote, "verified": "UNVERIFIED", "how": "verifier unavailable"}); continue
        try:
            r = verify(quote, cite)
        except Exception as e:
            r = {"status": "unverified", "how": f"verifier error {e.__class__.__name__}"}
        out.append(ground_departure({"cite": cite, "quote": quote, "verified": "[corpus]" if r.get("status") == "verified" else "NOT IN CORPUS",
                                     "chunk_id": r.get("chunk_id"), "how": r.get("how") or r.get("status")}, lookup))
    return out


def _cell_text(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("finding", "proposition", "gap", "fix", "title"))


# --- tie evidence (2026-10-24, SKEPTIC C8: ECF 145 tied to the Experian-never-served issue by a bot 'fix' sentence; the
# finding is in ECF 141's own text). CLASS RULE: an order is tied to an issue only when the tie is evidenced in that
# order's own text (dkt_<n> file / docket-sheet entry holds the issue's distinctive terms) or the matrix cell for that
# ECF carries the finding (names the issue number, or its FINDING restates the title) — never gap/fix speculation,
# never proximity. Every tie carries its evidence source into ISSUE_COVERAGE.json and the addendum sidecar.
_FINDING_FIELDS = ("finding", "proposition")


# words that make no issue distinctive (every order and every bot cell uses them) — the unique-token rule ignores them
_GENERIC = set("third circuit court appeal appeals appellate lack want claim claims dismiss dismissed dismissal dismissing "
               "grant granted granting deny denied denying motion motions order orders judgment error district federal "
               "rule rules filed filing finding findings issue issues plaintiff defendant defendants party parties case "
               "pleadings pleading complaint record under over without with state states text show notice relief time days leave".split())


def _stem(w: str) -> str:
    """Crude suffix stem so 'served' ~ 'service' ~ 'serve', 'contradicted' ~ 'contradicts' (no dictionary, no model)."""
    w = w.lower().replace("\u2019", "'")
    w = re.sub(r"'s$", "", w)
    for suf in ("ices", "ice", "ing", "ied", "ies", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


_GENERIC_STEMS = {_stem(w) for w in _GENERIC}


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z][a-z0-9'\u2019\-]{3,}", text.lower()) if w not in _STOP}


def unique_tokens(issues: dict) -> dict[int, set]:
    """Per issue, the title stems no other issue title carries and no generic legal word — the issue's distinctive
    terms ('experian', 'greensky', 'twombly')."""
    toks = {n: _stems(t) for n, t in issues.items()}
    return {n: {x for x in st if x not in _GENERIC_STEMS and not any(x in toks[m] for m in toks if m != n)} for n, st in toks.items()}


def issues_in_text(text: str, issues: dict, uniq: dict | None = None, k: int = 3, distinctive: bool = False) -> dict[int, str]:
    """{issue: the sentence that evidences it}: one sentence of `text` holding >= k title stems (at least two of them
    non-generic), or — `distinctive`, for an order's OWN words — >= 2 of which one is unique to that issue among all
    titles ('served Experian'). Stem-matched; generic legal words ('claim', 'dismissal', 'prejudice') never make a tie
    on their own. Bot prose gets only the k rule: it restates legal vocabulary, not the order."""
    uniq = uniq if uniq is not None else unique_tokens(issues)
    out = {}
    sents = [x for x in re.split(r"(?<=[.;:])\s+|\n", text) if x.strip()]
    for n, t in issues.items():
        want = _stems(t)
        if len(want) < 2:
            continue
        for sent in sents:
            hit = want & _stems(sent)
            if len(hit - _GENERIC_STEMS) >= 2 and (len(hit) >= k or (distinctive and hit & uniq.get(n, set()))):
                out[n] = " ".join(sent.split())[:240]
                break
    return out


def order_text(case_dir: Path, ecf: int) -> list[tuple[str, str]]:
    """[(source label, text)] of the order's OWN words: every intake dkt_<ecf>_*.txt sidecar that is the order itself
    (attachments 'Document n-m' and _partN scans excluded) plus the docket-sheet entry for that number."""
    out = []
    intake = Path(case_dir) / "intake"
    if not intake.is_dir():
        return out
    for t in sorted(intake.glob(f"dkt_{ecf}_*.txt")):
        if re.search(r"_part\d+", t.name):
            continue
        try:
            txt = t.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if re.search(r"Document \d+-\d+", txt[:300]):
            continue
        out.append((f"intake/{t.name}", txt))
    for sheet in sorted(intake.glob("*.txt")):
        if not re.search(r"docket", sheet.name, re.I) or "activity" in sheet.name.lower():
            continue
        try:
            txt = sheet.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(rf"(?m)^\d\d/\d\d/\d{{4}}\s+{ecf}\s+(.+?)(?=^\d\d/\d\d/\d{{4}}\s+\d+\s|\Z)", txt, re.S)
        if m:
            out.append((f"intake/{sheet.name}#entry-{ecf}", " ".join(m.group(1).split())))
            break
    return out


def _title_ecfs(title: str) -> list[int]:
    """A generated issue title 'ECF 130 (10/14/2025) Departed From …' names its order outright."""
    m = re.match(r"ECF\s+(\d+(?:/\d+)*)\s*\(", title)
    return [int(x) for x in m.group(1).split("/")] if m else []


def recompute_uncovered(cov: dict) -> dict:
    """C9 (2026-10-24): 'Orders not tied to any issue' = ALL orders minus the union of every tie held in by_issue — the
    same structure Part V renders — so the two lists cannot contradict. Also stamps each row's issues/status from the
    same union and asserts the invariant."""
    tied = {}
    for n, v in cov["by_issue"].items():
        for o in v["orders"]:
            tied.setdefault("/".join(map(str, o["ecfs"])), set()).add(int(n))
    for r in cov["orders"]:
        r["issues"] = sorted(tied.get(r["ecf"], set()))
        if r["issues"]:
            if not r["status"].startswith("COVERED"):
                r["status"] = "COVERED"
        else:
            r["status"] = "UNCOVERED" if r["cells"] else "UNANALYSED"
    cov["uncovered"] = [{"ecf": r["ecf"], "date": r["date"], "status": r["status"]} for r in cov["orders"] if not r["issues"]]
    assert not ({u["ecf"] for u in cov["uncovered"]} & set(tied)), "C9 invariant: an untied order is also tied"
    return cov


def _jsonl(p: Path) -> list[dict]:
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line)) if line.strip() else None
            except Exception:
                pass
    return rows


def issue_coverage(matrix_dir, issues_md, case_dir, bundle_dir=None, append=True, verify=None, lookup=None) -> dict:
    """order -> issues tied to it BY EVIDENCE (cell names the number / cell FINDING restates the title / the order's own
    text holds the issue's distinctive terms / the issue title names the order) -> rule quote verified and grounded;
    UNCOVERED orders (set difference, recompute_uncovered), NEW departures (verified quote, no issue holds it; appended
    to OUR_ISSUES.md as new issues, .bak beside it), and every all_orders() ECF missing from the units file. The dict is
    what Part V renders and the gate reads; `lookup` (chunk_id -> text) is injectable for tests."""
    matrix_dir, issues_md, case_dir = Path(matrix_dir), Path(issues_md), Path(case_dir)
    units_md = matrix_dir.with_suffix(".md") if matrix_dir.suffix == ".m2" else matrix_dir.parent / "ALL_ORDERS_UNITS.md"
    units = parse_units(units_md.read_text(encoding="utf-8", errors="replace")) if units_md.exists() else []
    issues = parse_issues(issues_md.read_text(encoding="utf-8", errors="replace")) if issues_md.exists() else {}
    cells = [c for c in _jsonl(matrix_dir / "cells.jsonl") if c.get("bot") == COVERAGE_BOT]
    verify = verify if verify is not None else _verify_phrase()
    uniq = unique_tokens(issues)
    rows, bogus_all, by_issue = [], {}, {n: [] for n in issues}
    for u in units:
        mine = [c for c in cells if int(c.get("claim") or 0) == u["unit"] or " ".join(str(c.get("title") or "").split()) == u["title"]]
        ties, bogus, deps = {}, set(), []          # issue -> {how, source, evidence}; first evidence wins
        for c in mine:
            a, b = issue_numbers_in(_cell_text(c), issues)
            bogus |= set(b)
            for n in a:
                ties.setdefault(n, {"how": "cell names the issue number", "source": f"cells.jsonl claim {c.get('claim')}", "evidence": ""})
            fnd = " ".join(str(c.get(k) or "") for k in _FINDING_FIELDS)
            for n, sent in issues_in_text(fnd, issues, uniq).items():
                ties.setdefault(n, {"how": "cell finding restates the issue title", "source": f"cells.jsonl claim {c.get('claim')}", "evidence": sent})
            deps += departures_of(c, verify, lookup)
        for e in u["ecfs"]:
            for src, txt in order_text(case_dir, e):
                for n, sent in issues_in_text(txt, issues, uniq, distinctive=True).items():
                    ties.setdefault(n, {"how": "order's own text holds the issue's distinctive terms", "source": src, "evidence": sent})
        for n, t in issues.items():
            if _title_ecfs(t) and set(_title_ecfs(t)) <= set(u["ecfs"]):
                ties.setdefault(n, {"how": "issue title names the order", "source": str(issues_md.name), "evidence": t})
        if bogus:
            bogus_all[u["unit"]] = sorted(bogus)
        seen, uniq_d = set(), []
        for d in deps:
            if (d["cite"], d["quote"][:60]) not in seen:
                seen.add((d["cite"], d["quote"][:60])); uniq_d.append(d)
        for n in sorted(ties):
            by_issue[n].append({"ecfs": u["ecfs"], "date": u["date"], **ties[n]})
        rows.append({"unit": u["unit"], "ecfs": u["ecfs"], "ecf": "/".join(map(str, u["ecfs"])), "date": u["date"], "title": u["title"],
                     "issues": sorted(ties), "ties": [{"issue": n, **ties[n]} for n in sorted(ties)], "departures": uniq_d,
                     "cells": [{"status": c.get("status"), "claim": c.get("claim")} for c in mine],
                     "status": "COVERED" if ties else ("UNCOVERED" if mine else "UNANALYSED")})
    cov = {"built": datetime.datetime.now().isoformat(timespec="seconds"), "bot": COVERAGE_BOT, "matrix": str(matrix_dir),
           "units_file": str(units_md), "issues_file": str(issues_md), "issues": {str(n): t for n, t in issues.items()}, "orders": rows,
           "by_issue": {str(n): {"title": issues[n], "orders": v} for n, v in by_issue.items()},
           "uncovered": [], "new_departures": [], "missing_from_units": [], "bogus_issue_numbers": bogus_all, "new_issues_appended": []}
    recompute_uncovered(cov)
    # a departure is NEW only when no listed issue already holds it (an issue whose title names this order and this
    # rule — same cite, or the cite it was relabelled from — is that issue, not a new one)
    def _held(r, d):
        for n, t in issues.items():
            if set(_title_ecfs(t)) == set(r["ecfs"]) and any(c and c.lower() in t.lower() for c in (d["cite"], d.get("relabelled_from"))):
                return True
        return False
    cov["new_departures"] = [{"ecf": r["ecf"], "date": r["date"], "cite": d["cite"], "quote": d["quote"], "chunk_id": d.get("chunk_id"),
                              "relabelled_from": d.get("relabelled_from")}
                             for r in rows if r["status"] != "COVERED" for d in r["departures"] if d["verified"] == "[corpus]" and not _held(r, d)]
    unit_ecfs = {e for u in units for e in u["ecfs"]}
    cov["missing_from_units"] = [{"ecf": e, "date": d, "title": t} for e, d, t in all_orders(case_dir, bundle_dir) if e not in unit_ecfs]
    if append and cov["new_departures"] and issues_md.exists():
        _append_new_issues(issues_md, cov)
    recompute_uncovered(cov)
    new, missing = cov["new_departures"], cov["missing_from_units"]
    c = cov["counts"] = {"orders": len(rows), "covered": sum(1 for r in rows if r["status"].startswith("COVERED")),
                         "uncovered": len(cov["uncovered"]), "issues": len(cov["issues"]),
                         "issues_with_orders": sum(1 for v in cov["by_issue"].values() if v["orders"]),
                         "new_departures": len(new), "missing_from_units": len(missing)}
    return cov


def _append_new_issues(issues_md: Path, cov: dict):
    """NEW departures -> new numbered issues at the end of OUR_ISSUES.md (.bak.<TS> beside it). Idempotent by title."""
    import shutil
    txt = issues_md.read_text(encoding="utf-8")
    nxt = (max(parse_issues(txt)) + 1) if parse_issues(txt) else 1
    body, added = txt.rstrip("\n") + "\n", []
    for d in cov["new_departures"]:
        title = f"ECF {d['ecf']} ({d['date']}) Departed From {d['cite']}"
        old_title = f"ECF {d['ecf']} ({d['date']}) Departed From {d['relabelled_from']}" if d.get("relabelled_from") else ""
        if title.lower() in txt.lower() or (old_title and old_title.lower() in txt.lower()) or any(a["title"] == title for a in added):
            continue
        body += (f"\n**{nxt}. {title}**\n\nRecord facts: The order at ECF {d['ecf']} ({d['date']}) departed from {d['cite']}: "
                 f"\u201c{d['quote']}\u201d [corpus:{d.get('chunk_id') or '?'}]. Found by {COVERAGE_BOT}; no as-mailed issue named it.\n")
        added.append({"n": nxt, "title": title, "ecf": d["ecf"], "cite": d["cite"]})
        cov["issues"][str(nxt)] = title
        cov["by_issue"][str(nxt)] = {"title": title, "orders": [{"ecfs": [int(x) for x in re.findall(r"\d+", d["ecf"])], "date": d["date"],
                                                                "how": "issue title names the order", "source": issues_md.name, "evidence": title}], "new": True}
        for r in cov["orders"]:
            if r["ecf"] == d["ecf"]:
                r["status"] = "COVERED(new)"
                r["ties"].append({"issue": nxt, "how": "issue title names the order", "source": issues_md.name, "evidence": title})
        nxt += 1
    if added:
        shutil.copy2(issues_md, issues_md.with_name(issues_md.name + ".bak." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
        issues_md.write_text(body, encoding="utf-8")
    cov["new_issues_appended"] = added          # uncovered is recomputed by the caller from by_issue (C9), never edited here


QUOTE_CAP = 320      # soft cap: the quote is cut back to a sentence end within it, never mid-sentence (C4)


def issues_preserved(cov: dict) -> list[str]:
    """Part V rows: every issue number+title, the orders (ECF n, date) it reaches, the rule/statute departed from (cite +
    sentence-complete quote when card_gate.verify_phrase verified it, else the cite marked [NOT IN CORPUS]); then the
    orders no issue reaches (recompute_uncovered: set difference of the same ties, C9) and the departures matching no
    listed issue, stated outright — silence is waiver, a listed gap is not."""
    recompute_uncovered(cov)
    deps_by_ecf = {r["ecf"]: r["departures"] for r in cov["orders"]}
    relabel = {d["relabelled_from"]: d["cite"] for ds in deps_by_ecf.values() for d in ds if d.get("relabelled_from")}
    out = ["Every order listed in Part II merged into the final judgment and is before this Court (Fed. R. App. P. 3(c)(4)). "
           "Each issue below is preserved as to each order it reaches; nothing is waived by grouping."]
    tied_anywhere = set()
    for n, v in cov["by_issue"].items():
        ords = [f"ECF {'/'.join(map(str, o['ecfs']))} ({o['date']})" for o in v["orders"]]
        tied_anywhere |= {"/".join(map(str, o["ecfs"])) for o in v["orders"]}
        title = normalize_title(v["title"])
        for old_c, new_c in relabel.items():      # a generated title carries the cell's label; the chunk's governs (C5)
            if _title_ecfs(title) and old_c in title:
                title = title.replace(old_c, new_c)
        rules, seen = [], set()
        for o in v["orders"]:
            for d in deps_by_ecf.get("/".join(map(str, o["ecfs"])), []):
                if d["cite"] in seen:
                    continue
                if _title_ecfs(title) and d["cite"] not in title and d.get("relabelled_from", "\0") not in title:
                    continue                  # a generated 'Departed From X' issue is about X, not the order's other cites
                seen.add(d["cite"])
                rules.append(f"{d['cite']}: \u201c{_clip_sentences(d['quote'], QUOTE_CAP)}\u201d" if d["verified"] == "[corpus]"
                             else f"{d['cite']} [{'UNVERIFIED' if d['verified'] == 'UNVERIFIED' else 'NOT IN CORPUS'}]")
        out.append(f"Issue {n} \u2014 {title}. Orders reached: " + (", ".join(ords) if ords else "the final judgment as a whole") +
                   ". Rule departed from: " + ("; ".join(rules) if rules else "as stated in the argument") + ".")
    untied = [u for u in cov["uncovered"]]
    assert not ({u["ecf"] for u in untied} & tied_anywhere), "C9: Part V would list a tied order as untied"
    out.append("Orders not tied to any issue by the record analysis: " +
               ("; ".join(f"ECF {u['ecf']} ({u['date']})" for u in untied) + " \u2014 each preserved here expressly." if untied else "none."))
    out.append("Departures found that match no listed issue: " +
               ("; ".join(f"ECF {d['ecf']} ({d['date']}) \u2014 {d['cite']}: \u201c{_clip_sentences(d['quote'], QUOTE_CAP)}\u201d" for d in cov["new_departures"]) +
                " (each added as a preserved issue above)." if cov["new_departures"] else "none."))
    if cov["missing_from_units"]:
        out.append("ORDERS ON THE DOCKET NOT ANALYSED: " + "; ".join(f"ECF {m['ecf']} ({m['date']})" for m in cov["missing_from_units"]) + ".")
    return out


def coverage_report(cov: dict) -> str:
    """ISSUE_COVERAGE.md — a REPORT for the user (not a pipeline input)."""
    c = cov["counts"]
    L = [f"# ISSUE COVERAGE — every adverse order -> the issues that reach it (built {cov['built']}, bot {cov['bot']})", "",
         f"Orders {c['orders']} | covered {c['covered']} | UNCOVERED {c['uncovered']} | issues {c['issues']} ({c['issues_with_orders']} reach an order) | "
         f"new departures {c['new_departures']} | orders missing from units {c['missing_from_units']}", "",
         "| ECF | date | issues implicated | rule/statute departed from | bot cells |", "|---|---|---|---|---|"]
    for r in cov["orders"]:
        deps = "; ".join(f"{d['cite']} {d['verified']}" for d in r["departures"]) or "—"
        L.append(f"| {r['ecf']} | {r['date']} | {', '.join(map(str, r['issues'])) or '**NONE — ' + r['status'] + '**'} | {deps} | {len(r['cells'])} |")
    L += ["", "## Tie evidence (C8: every tie names its source)", ""] + [f"- ECF {r['ecf']} -> issue {t['issue']}: {t['how']} [{t['source']}] {t['evidence'][:120]}"
                                                                        for r in cov["orders"] for t in r.get("ties", [])]
    L += ["", "## Per issue", ""] + [f"- **{n}. {v['title'][:110]}** -> " + (", ".join(f"ECF {'/'.join(map(str, o['ecfs']))} ({o['date']})" for o in v["orders"]) or "(no order)")
                                     for n, v in cov["by_issue"].items()]
    L += ["", "## UNCOVERED orders", ""] + ([f"- ECF {u['ecf']} ({u['date']}) [{u['status']}]" for u in cov["uncovered"]] or ["- none"])
    L += ["", "## NEW departures", ""] + ([f"- ECF {d['ecf']} ({d['date']}): {d['cite']} — \u201c{d['quote'][:120]}\u201d" for d in cov["new_departures"]] or ["- none"])
    if cov["new_issues_appended"]:
        L.append("Appended to OUR_ISSUES.md: " + ", ".join(f"{a['n']} ({a['title']})" for a in cov["new_issues_appended"]))
    L += ["", "## all_orders() ECFs missing from ALL_ORDERS_UNITS.md", ""] + ([f"- ECF {m['ecf']} ({m['date']}) {m['title']}" for m in cov["missing_from_units"]] or ["- none"])
    if cov["bogus_issue_numbers"]:
        L.append(f"\nIssue numbers named by the bot that OUR_ISSUES.md does not hold (dropped): {cov['bogus_issue_numbers']}")
    return "\n".join(L) + "\n"


def _text_pages(title_lines: list[str], sections: list[tuple[str, list[str]]], out_pdf: Path):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from xml.sax.saxutils import escape
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName="Times-Roman", fontSize=12, leading=15, spaceAfter=6)
    head = ParagraphStyle("h", parent=body, fontName="Times-Bold", alignment=1, spaceAfter=10)
    sub = ParagraphStyle("s", parent=body, fontName="Times-Bold", spaceBefore=8)
    flow = [Paragraph(escape(t), head) for t in title_lines] + [Spacer(1, 8)]
    for h, lines in sections:
        flow.append(Paragraph(escape(h), sub))
        flow += [Paragraph(escape(x), body) for x in lines]
    SimpleDocTemplate(str(out_pdf), pagesize=letter, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72).build(flow)


def _orders_matrix(filing_md: Path, orders_matrix=None):
    """The all-orders matrix dir: explicit arg, else $M1_ORDERS_MATRIX (run_screen.sh exports it; m1 calls build()
    without the arg), else <bundle>/m2/ALL_ORDERS_UNITS.m2 when its cells exist."""
    import os
    cand = orders_matrix or os.environ.get("M1_ORDERS_MATRIX") or (filing_md.parent.parent.parent / "m2" / "ALL_ORDERS_UNITS.m2")
    return Path(cand) if cand and (Path(cand) / "cells.jsonl").exists() else None


def build(filing_md, stub_path, cells_path=None, orders_matrix=None) -> str:
    from pypdf import PdfWriter, PdfReader
    from pipeline import m1_screen as _s
    filing_md = Path(filing_md)
    _mx = _orders_matrix(filing_md, orders_matrix)
    cov = None
    if _mx:   # Part V ISSUES PRESERVED: the all-orders matrix + OUR_ISSUES.md (stage 0); also writes the ISSUE_COVERAGE.md report
        cov = issue_coverage(_mx, _mx.parent / "OUR_ISSUES.md", _case_dir(stub_path) or _case_dir(filing_md), filing_md.parent.parent.parent)
        (_mx.parent / "ISSUE_COVERAGE.md").write_text(coverage_report(cov), encoding="utf-8")
        (_mx.parent / "ISSUE_COVERAGE.json").write_text(json.dumps(cov, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  [addendum] Part V: orders {cov['counts']['orders']} covered {cov['counts']['covered']} UNCOVERED {cov['counts']['uncovered']} "
              f"new {cov['counts']['new_departures']} missing-from-units {cov['counts']['missing_from_units']}")
    else:
        print("  [addendum] Part V: no all-orders matrix (stage 0 not run) — ISSUES PRESERVED lists the argument's own issues only")
    md = filing_md.read_text(encoding="utf-8")
    case_dir = _case_dir(stub_path) or _case_dir(filing_md)
    intake = case_dir / "intake"
    cap, appno = _s.caption_of(Path(stub_path).read_text(encoding="utf-8", errors="replace"))
    _fdm = re.search(r"\d+", _s.final_dkt_of(Path(stub_path).read_text(encoding="utf-8", errors="replace")) or "")
    _fd = int(_fdm.group(0)) if _fdm else 0
    cap_line = re.sub(r"\s*\[[^\]]*\]\s*$", "", cap).strip()
    dk = cited_dockets(md)
    held = [(n, _pdf_for(n, intake)) for n in dk]
    sheet = _docket_sheet(intake)
    exh = [(L, on, _exhibit_pdf(L, case_dir)) for L, on in cited_exhibits(md)]
    quoted = []
    for c, q in quoted_rules(md):   # Part IV prints only quotes the corpus attributes to the cited authority
        ok = attributed(c, q)
        if ok is False:
            print(f"  [addendum] dropped misattributed quote: {c}: \"{q[:60]}\" (found only in another authority's chunk)")
            continue
        quoted.append((c, q))
    cq = _cell_quotes(cells_path) if cells_path else {}
    have = {c for c, _q in quoted}
    for c in cited_rules(md):   # cell quotes go through the same attribution gate — a bot's mislabel is no better than a drafter's
        if c not in have and c in cq:
            if attributed(c, cq[c]) is False:
                print(f"  [addendum] dropped misattributed cell quote: {c}: \"{cq[c][:60]}\"")
                continue
            quoted.append((c, cq[c]))
    idx = [f"Dkt. No. {n} — " + ("INCLUDED" if p else "on the district court docket (not held as a PDF)") for n, p in held]
    idx.append("District court docket sheet — " + ("INCLUDED" if sheet else "on the district court docket"))
    for L, on, p in exh:
        where = f" (attached to Dkt. No. {on})" if on else ""
        idx.append(f"Exhibit {L}{where} — " + ("INCLUDED" if p else "in the district court record"))
    rules = [f"{c}: \u201c{q}.\u201d" for c, q in quoted] or ["(The argument quotes no rule or statute.)"]
    stem = re.sub(r"_(DRAFT|FILING)$", "", filing_md.stem)
    front = filing_md.with_name(stem + "_ADDENDUM_front.pdf")
    _secs = [("WHAT THE RECORD IS", [
                    # verified verbatim [corpus:amjur2d_32A_volume_32a#acc2bca6537cd753]; a paper delivered to the clerk
                    # is filed (Fed. R. Civ. P. 5(d)(2)) and so is part of it, docketed or not
                    "\u201cThe record on appeal consists of the original papers and exhibits filed in the District Court, the "
                    "transcript of proceedings (if any), and a certified copy of the docket entries prepared by the clerk of the "
                    "District Court.\u201d 32A Am. Jur. 2d Federal Practice and Procedure \u00a7 1101; see Fed. R. App. P. 10(a). "
                    "Each document below was filed in, or delivered to the Clerk of, the District Court."]),
                 ("I. THE ORDER THIS ARGUMENT ANSWERS (verbatim, as served)", order_answered(intake, appno) or
                  ["(No scan of the screening order is in the case intake.)"]),
                 ("II. EVERY ORDER ENTERED BELOW (each merged in the final judgment and before this Court)",
                  [f"ECF {n} ({d}) \u2014 {t}" + (" [post-judgment]" if _fd and n > _fd else "")
                   for n, d, t in all_orders(case_dir, filing_md.parent.parent.parent)] or
                  ["(No docket index or order PDFs found in the case.)"]),
                 ("III. INDEX OF RECORD DOCUMENTS CITED", idx),
                 ("IV. PERTINENT RULES AND STATUTES (verbatim as quoted in the argument)", rules),
                 ("V. ISSUES PRESERVED \u2014 every issue of record and every order it reaches",
                  issues_preserved(cov) if cov else
                  [f"Issue {n} \u2014 {h}. Orders reached: " + (", ".join(f"ECF {e}" for e in ecfs) or "the final judgment as a whole") + "."
                   for n, h, ecfs in issue_rows(md)] or ["(The argument states no numbered issues.)"]),
                 ("VI. FINDINGS REQUESTED, ISSUE BY ISSUE", findings_requested(issue_rows(md)))]
    # text twin of the addendum beside the draft: the gate's WAIVER check reads Part V from it (the PDF is for the court)
    filing_md.with_name(stem + "_ADDENDUM.md").write_text(
        "\n".join([cap_line or "ADDENDUM", "ADDENDUM TO APPELLANT'S ARGUMENT", ""] + [x for h, ls in _secs for x in ["## " + h, *ls, ""]]), encoding="utf-8")
    _text_pages([cap_line or "ADDENDUM", "ADDENDUM TO APPELLANT'S ARGUMENT",
                 "(record documents cited and pertinent rule text; not part of the argument's page count)"],
                _secs, front)
    w = PdfWriter()
    for pg in PdfReader(str(front)).pages:
        w.add_page(pg)
    used = []
    for p in [p for _n, p in held if p] + ([sheet] if sheet else []) + [p for _L, _o, p in exh if p]:
        if p in used:
            continue
        used.append(p)
        try:
            for pg in PdfReader(str(p)).pages:
                w.add_page(pg)
        except Exception as e:
            print(f"  [addendum] could not read {p.name}: {e}")
    out = filing_md.with_name(stem + "_ADDENDUM.pdf")
    with open(out, "wb") as f:
        w.write(f)
    front.unlink(missing_ok=True)
    side = {"built": datetime.datetime.now().isoformat(timespec="seconds"), "appellate_no": appno,
            "dockets_cited": dk, "held": {n: (p.name if p else None) for n, p in held},
            "docket_sheet": sheet.name if sheet else None,
            "exhibits": [{"letter": L, "on": on, "file": p.name if p else None} for L, on, p in exh],
            "rules": [{"cite": c, "quote": q} for c, q in quoted], "pdf": str(out),
            "issues_preserved": ({"orders_matrix": str(_mx), "counts": cov["counts"], "uncovered": cov["uncovered"], "issues": list(cov["issues"]),
                                  "orders": [r["ecf"] for r in cov["orders"]],
                                  "ties": [{"ecf": r["ecf"], **t} for r in cov["orders"] for t in r.get("ties", [])],
                                  "new_issues_appended": cov["new_issues_appended"]} if cov else None)}
    out.with_suffix(".json").write_text(json.dumps(side, indent=1, ensure_ascii=False), encoding="utf-8")
    n_pages = len(PdfReader(str(out)).pages)
    return (f"{out.name}: {n_pages} pp; cited {len(dk)} docket entries, {sum(1 for _n, p in held if p)} held; "
            f"exhibits {len(exh)} ({sum(1 for *_x, p in exh if p)} held); rules quoted {len(quoted)}")


def main(argv=None) -> int:
    """`python3 -m pipeline.record_addendum --orders-matrix <ALL_ORDERS_UNITS.m2> --issues <OUR_ISSUES.md> --case-dir <dir>`:
    stage-0 report — writes ISSUE_COVERAGE.md/.json beside the matrix (the same tabulation Part V renders) and exits 1 when
    an order is UNCOVERED or missing from the units, so the chain log shows the waiver risk before any draft is cut."""
    import argparse, sys
    ap = argparse.ArgumentParser(description="issue-coverage report from the all-orders matrix")
    ap.add_argument("--orders-matrix", required=True); ap.add_argument("--issues", required=True); ap.add_argument("--case-dir", required=True)
    ap.add_argument("--bundle", default=None); ap.add_argument("--no-append", action="store_true", help="report NEW departures without appending to OUR_ISSUES.md")
    a = ap.parse_args(argv)
    mx = Path(a.orders_matrix)
    cov = issue_coverage(mx, a.issues, a.case_dir, a.bundle or mx.parent.parent, append=not a.no_append)
    (mx.parent / "ISSUE_COVERAGE.md").write_text(coverage_report(cov), encoding="utf-8")
    (mx.parent / "ISSUE_COVERAGE.json").write_text(json.dumps(cov, indent=1, ensure_ascii=False), encoding="utf-8")
    c = cov["counts"]
    print(f"[coverage] {mx.parent / 'ISSUE_COVERAGE.md'}: orders {c['orders']} covered {c['covered']} UNCOVERED {c['uncovered']} | "
          f"issues {c['issues']} | new {c['new_departures']} (appended {len(cov['new_issues_appended'])}) | missing-from-units {c['missing_from_units']}")
    for u in cov["uncovered"]:
        print(f"[coverage]   UNCOVERED ECF {u['ecf']} ({u['date']}) [{u['status']}]")
    for m in cov["missing_from_units"]:
        print(f"[coverage]   MISSING FROM UNITS ECF {m['ecf']} ({m['date']})")
    return 1 if (c["uncovered"] or c["missing_from_units"]) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
