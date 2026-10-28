#!/usr/bin/env python3
"""pipeline/statement_ledger.py — the memory organ. Captures EVERY statement.

WHY (2026-08-25, user-directed): the trick-catcher speaks only when statements
CONFLICT. But the stop-filing order was not a contradiction — it was a court
statement sitting in the record that nothing answered. The missing allonge was
first visible as an opponent claim ("PHL is the holder") that our filings had
not yet met with the record proof. This organ captures ALL statements — the
opponent's, the court's, ours, and chat-session findings — and computes
REBUTTAL COVERAGE: which adverse statements are UNREBUTTED. That queue is what
m0 must answer in the next filing. Nothing depends on the user spotting it.

WHAT IT CAPTURES per case:
  role assertions      (trick_catcher.extract_role_assertions — all 39-cat vocab)
  fact ledger entries  (case_fact_comparator.extract_fact_ledger — amounts/dates)
  court directives     (ORDERED / shall / must / DENIED / DISMISSED sentences)
  claim sentences      (holder/owed/denied/discharged/entitled/default claims)
  chat findings        (output/learnings.jsonl entries tagged with the case)

AUTHOR BUCKETING: pipeline.reasoning_layer.detect_document_author (the
authorship guardrail) — pro_se / opponent / court / unknown.

REBUTTAL COVERAGE (honest heuristic, flagged as such): an adverse statement is
ADDRESSED if some pro-se-authored document shares enough of its salient tokens
(incl. any number tokens); else UNREBUTTED — a queue item, not a verdict.

Output: obsidian_sync/cases/<slug>/_STATEMENT_LEDGER.json + .md (linked).
CLI: python3 -m pipeline.statement_ledger [case ...]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
CASES_DIR = ROOT / "obsidian_sync" / "cases"
LEARNINGS = ROOT / "output" / "learnings.jsonl"

CASES = ["fed_26-cv-00123", "fed_26-cv-00456", "fed_26-cv-00789",
         "phila_ccp_260100000"]

_DIRECTIVE_RX = re.compile(
    r"([^.\n]{0,200}?\b(?:IT IS (?:HEREBY )?ORDERED|shall not|shall\b|must\b|"
    r"is (?:hereby )?(?:DENIED|GRANTED|DISMISSED|STRICKEN)|"
    r"ORDERED that)[^.\n]{0,240})", re.I)
_CLAIM_RX = re.compile(
    r"([^.\n]{0,160}?\b(?:is the (?:current |new )?(?:holder|owner|creditor|"
    r"servicer|insurer|real party)|is owed|are owed|owes\b|"
    r"debt (?:is|was) (?:discharged|cancelled|owed|valid)|"
    r"in default|is entitled to enforce|denied? (?:the |your )?claim|"
    r"coverage (?:is|was) (?:denied|excluded)|"
    r"lacks? standing|fail(?:s|ed) to state)[^.\n]{0,200})", re.I)
_STOP = set("the this that with from have been were their which would could "
            "should there where when what your our and for are was not has "
            "had may can will".split())
# litigation-generic tokens: present in virtually every filing, so they carry
# ZERO rebuttal signal — a rebuttal is proven by DISTINCTIVE tokens (names,
# amounts, dates, specific instruments), never by shared boilerplate.
_GENERIC = set("plaintiff plaintiffs defendant defendants court motion motions "
               "order ordered further hereby pursuant against under filed "
               "filing shall claim claims count counts case matter action "
               "united states district eastern pennsylvania judge honorable "
               "granted denied dismissed stricken pending remains before "
               "response opposition reply brief memorandum exhibit".split())


def _read(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _author(fp: Path, text: str) -> str:
    """pro_se / opponent / court / unknown via the authorship guardrail."""
    a = "unknown"
    try:
        from pipeline.reasoning_layer import detect_document_author
        a = detect_document_author(text) or "unknown"
    except Exception:
        pass
    if a == "unknown":
        # filename fallback (sidecar names carry strong signals)
        n = fp.name.lower()
        if any(k in n for k in ("order", "ecf", "opinion", "judgment",
                                "docket")):
            return "court"
        if any(k in n for k in ("filer", "rebuttal", "motion to remand",
                                "motion to remove", "response to",
                                "opposition to")):
            return "pro_se"
        if any(k in n for k in ("reply", "answer", "notice of removal",
                                "defense", "defendant")):
            return "opponent"
    return a


def _salient(s: str, n: int = 8) -> list:
    toks = [t.lower() for t in re.findall(r"[A-Za-z]{5,}|\d[\d,\.]{3,}", s)]
    toks = [t for t in toks if t not in _STOP and t not in _GENERIC]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:n]


def _sidecars(idir: Path):
    for fp in sorted(idir.rglob("*")):
        if fp.is_file() and fp.name.lower().endswith((".doctext.txt",
                                                      ".ocr.txt")):
            yield fp


def build_ledger(case_slug: str, max_docs: int = 500) -> dict:
    sys.path.insert(0, str(ROOT))
    from pipeline import trick_catcher as tc
    from pipeline import case_fact_comparator as cfc

    idir = CASES_DIR / case_slug / "intake"
    # BASELINE 2026-08-25: evidence/ carries exhibits (unlocked 1099-Cs, IRS
    # transcripts) the organs MUST read — intake/ alone missed the loan
    # 1481262085 1099-C sitting in evidence/
    extra_dirs = [d for d in (CASES_DIR / case_slug / "evidence",)
                  if d.is_dir()]
    ledger = {"case": case_slug,
              "generated": datetime.now().isoformat(timespec="seconds"),
              "statements": [], "unrebutted": [], "counts": {},
              "note": ("rebuttal status is a deterministic token-overlap "
                       "heuristic — a queue for m0, not a legal verdict")}
    if not idir.is_dir():
        ledger["counts"] = {"error": f"no intake dir: {idir}"}
        return ledger

    pro_se_texts = []
    docs = list(_sidecars(idir))
    for xd in extra_dirs:
        docs += list(_sidecars(xd))
    docs = docs[:max_docs]
    for fp in docs:
        text = _read(fp)
        if len(text.strip()) < 200:
            continue
        author = _author(fp, text)
        src = fp.name[:80]
        if author == "pro_se":
            pro_se_texts.append(text.lower())

        for a in tc.extract_role_assertions(text, src, case_slug):
            ledger["statements"].append(
                {"kind": "role", "author": author, "source": src,
                 "party": a.get("party"), "role": a.get("role"),
                 "loan": a.get("loan"), "text": a.get("quote", "")[:260]})
        for f in cfc.extract_fact_ledger(text, src):
            ledger["statements"].append(
                {"kind": "fact", "author": author, "source": src,
                 "fact_type": f.get("fact_type"), "loan": f.get("loan"),
                 "value": str(f.get("value", ""))[:60],
                 "text": str(f.get("quote", ""))[:260]})
        rx = _DIRECTIVE_RX if author == "court" else _CLAIM_RX
        seen = set()
        for m in rx.finditer(text):
            s = " ".join(m.group(1).split())[:300]
            k = s[:120].lower()
            if k in seen or len(s) < 40:
                continue
            seen.add(k)
            ledger["statements"].append(
                {"kind": "directive" if author == "court" else "claim",
                 "author": author, "source": src, "text": s})
            if len(seen) >= 25:
                break

    # chat capture: learnings tagged with this case
    tag_variants = {case_slug, case_slug.replace("fed_", "fed:2:"),
                    case_slug.replace("_", ":")}
    if LEARNINGS.exists():
        for line in _read(LEARNINGS).splitlines():
            try:
                j = json.loads(line)
            except Exception:
                continue
            tags = set(j.get("tags", []))
            if tags & tag_variants or any(case_slug in str(t) for t in tags):
                ledger["statements"].append(
                    {"kind": "chat_finding", "author": "pro_se_agent",
                     "source": j.get("source", "chat")[:80],
                     "text": str(j.get("text", ""))[:300]})

    # rebuttal coverage over adverse statements
    corpus = "\n".join(pro_se_texts)
    for s in ledger["statements"]:
        if s["author"] not in ("opponent", "court", "unknown"):
            continue
        if s["kind"] in ("chat_finding",):
            continue
        toks = _salient(s["text"])
        if not toks:
            continue
        hit = sum(1 for t in toks if t in corpus)
        s["rebuttal"] = "ADDRESSED" if (toks and hit / len(toks) >= 0.6) \
            else "UNREBUTTED"
        if s["rebuttal"] == "UNREBUTTED":
            ledger["unrebutted"].append(s)

    c = Counter()
    for s in ledger["statements"]:
        c[s["author"]] += 1
        c["kind_" + s["kind"]] += 1
    c["unrebutted"] = len(ledger["unrebutted"])
    ledger["counts"] = dict(c)
    return ledger


def write_ledger(case_slug: str, max_docs: int = 500) -> dict:
    led = build_ledger(case_slug, max_docs=max_docs)
    cdir = CASES_DIR / case_slug
    if cdir.is_dir():
        jp = cdir / "_STATEMENT_LEDGER.json"
        jp.write_text(json.dumps(led, indent=1), encoding="utf-8")
        mp = cdir / "_STATEMENT_LEDGER.md"
        lines = [f"# Statement Ledger — {case_slug}",
                 f"*generated {led['generated']} — rebuttal status is a "
                 "heuristic queue for m0, not a verdict*", "",
                 f"**Counts:** {json.dumps(led['counts'])}", "",
                 "## UNREBUTTED adverse statements (answer these next filing)",
                 ""]
        for s in led["unrebutted"][:60]:
            lines.append(f"- **[{s['author']}/{s['kind']}]** "
                         f"`{s['source'][:50]}` — {s['text'][:220]}")
        lines += ["", "Full register: `_STATEMENT_LEDGER.json` — "
                  "[[_TRICK_INVENTORY]] [[CASE_FACTS]]"]
        mp.write_text("\n".join(lines), encoding="utf-8")
        led["_json"] = str(jp)
        led["_md"] = str(mp)
    return led


def main() -> int:
    for slug in (sys.argv[1:] or CASES):
        led = write_ledger(slug)
        print(f"[{slug}] statements={len(led['statements'])} "
              f"unrebutted={len(led['unrebutted'])} counts={led['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
