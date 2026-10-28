#!/usr/bin/env python3
"""m0_matter_brief — the PRE-FLIGHT organ. Deterministically assembles the FULL
matter context BEFORE any m0 drafting call, so the user never re-explains what
the vault already knows.

USER (2026-09-02, verbatim trigger): "like the whole context is there I don't
know why I have to keep re-explaining... are you making a task list of checks
before the m0 starts work or is this an engine the m0 and you are missing."

WHAT IT DOES (no LLM — pure graph walk + extraction):
  For a case slug or matter id, emit MATTER_BRIEF.md into the case folder:
    1. IDENTITY      — INITIAL CLAIM section (the case's own identity block)
    2. POSTURE       — newest posture lines from CASE_FACTS + live deadlines
    3. USER-LOCKED   — every line in vault notes tagged (user-locked / user:
                       verbatim / STRATEGY (locked...)) that wikilinks to or
                       mentions this matter — the decisions the user already made
    4. INSTRUMENTS   — every draft/sent instrument in drafts/ + outbox jobs +
                       notices (what has ALREADY been sent; posture, not blank
                       slate)
    5. DOCTRINE      — analysis/ notes reachable by [[wikilink]] from the case
                       files or naming the matter's key terms (the deep layer:
                       dishonor/coupon/ROE/etc.)
    6. APPEAL SCOPE  — notices of appeal found in intake: what was appealed,
                       verbatim, so no downstream filing narrows it
  Every m0 drafting call takes MATTER_BRIEF.md as mandatory context.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
VAULT = ROOT / "obsidian_sync"
ANALYSIS = VAULT / "analysis"

LOCK_PAT = re.compile(
    r"(?i)(user-locked|user['\u2019]s own words|verbatim|STRATEGY \(locked|LOCKED \d{4}|"
    r"locked \d{4}-\d{2}-\d{2}|user directive|user corrected|user:)")
APPEAL_PAT = re.compile(r"(?i)notice of appeal")


def _read(p: Path, n: int = 200000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:n]
    except Exception:
        return ""


def _section(text: str, header: str, cap: int = 6000) -> str:
    m = re.search(rf"^#+\s*{re.escape(header)}.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0)[:cap] if m else ""


def _wikilinks(text: str) -> set[str]:
    return {m.group(1).split("|")[0].split("/")[-1].strip()
            for m in re.finditer(r"\[\[([^\]]+)\]\]", text)}


def build(slug: str) -> Path:
    case_dir = VAULT / "cases" / slug
    if not case_dir.exists():
        raise SystemExit(f"no case dir: {case_dir}")
    cf_text = _read(case_dir / "CASE_FACTS.md")
    parts: list[str] = [
        f"# MATTER BRIEF — {slug}",
        f"*m0_matter_brief, {datetime.now():%Y-%m-%d %H:%M}. Deterministic pre-flight context. "
        "EVERY m0 drafting call for this matter MUST consume this file — the user does not re-explain.*",
        "",
    ]

    # 1. IDENTITY
    ic = _section(cf_text, "INITIAL CLAIM")
    parts += ["## 1. IDENTITY (INITIAL CLAIM)", "", ic or "(no INITIAL CLAIM section — run case_facts_refresh aff_initial)", ""]

    # 2. POSTURE — dated posture lines, newest first
    postures = re.findall(r"^- Posture \([^)]*\):.*(?:\n(?!- ).*)*", cf_text, re.M)
    parts += ["## 2. POSTURE (newest first)", ""] + (postures[:6] or ["(none recorded)"]) + [""]

    # 2b. FEE / IFP / GRANT STATUS (2026-09-02 class fix: the 26-0000
    # IFP-GRANTED docket entries sat in CASE_TIMELINE.json while the chat model
    # reasoned from a clerk letter instead — status lines the record already
    # DECIDED must surface in the brief so nothing re-litigates them).
    status_hits: list[str] = []
    seen_s: set[str] = set()
    pat = re.compile(r"(?i)(IFP GRANTED|IFP DENIED|forma pauperis[^\"\n]{0,60}(?:granted|denied)|"
                     r"fee (?:paid|waived)|54\(b\)|certif[^\"\n]{0,40}good faith)")
    for src_p in (case_dir / "CASE_TIMELINE.json", case_dir / "CASE_FACTS.md"):
        txt = _read(src_p, 400_000)
        for m in pat.finditer(txt):
            s, e = max(0, m.start() - 160), min(len(txt), m.end() + 60)
            frag = re.sub(r"[\\\"{}\[\]]+", " ", txt[s:e])
            frag = re.sub(r"\s+", " ", frag).strip()
            key = m.group(0).upper()[:30]
            if key in seen_s:
                continue
            seen_s.add(key)
            status_hits.append(f"- [{src_p.name}] …{frag}…")
    parts += ["## 2b. FEE / IFP / GRANT STATUS (record-decided — do not re-litigate)", ""] + \
             (status_hits[:8] or ["(no status entries found)"]) + [""]

    # 3. USER-LOCKED decisions — from case files AND analysis notes that mention the matter
    locked: list[str] = []
    # key terms: slug tokens are useless for federal dockets (v-sandbox fire:
    # doctrine=0) — derive from the INITIAL CLAIM's own claim-type words
    key_terms = set(re.findall(r"[A-Za-z][A-Za-z-]{5,}", slug.lower())) | {slug}
    _ic_low = ic.lower()
    for kt in ("fcra", "credit report", "fdcpa", "quiet title", "chain of title",
               "respa", "mortgage", "garnish", "bad faith", "insurance", "refund",
               "coupon", "dishonor", "tender", "discharge", "revocation"):
        if kt in _ic_low or kt in cf_text.lower()[:12000]:
            key_terms.add(kt)
    for f in list(case_dir.glob("*.md")) + list(case_dir.glob("drafts/*.md")) + list(ANALYSIS.glob("*.md")):
        t = _read(f, 60000)
        rel = (f.parent.name != "analysis") or any(k in t.lower() for k in key_terms)
        if not rel:
            continue
        for ln in t.splitlines():
            if LOCK_PAT.search(ln) and len(ln.strip()) > 30:
                locked.append(f"- [{f.name}] {ln.strip()[:300]}")
    seen = set()
    locked = [x for x in locked if not (x in seen or seen.add(x))]
    parts += ["## 3. USER-LOCKED DECISIONS (never re-ask; never contradict)", ""] + (locked[:25] or ["(none found)"]) + [""]

    # 4. INSTRUMENTS already drafted/sent — FINAL/DRAFT/packet artifacts only,
    # not every versioned intermediate (v-sandbox fire: 281 rows of noise)
    inst: list[str] = []
    seen_kind: set[str] = set()
    for f in sorted(case_dir.glob("drafts/*.md"), reverse=True) + sorted(case_dir.glob("drafts/**/*_DRAFT.md"), reverse=True):
        if not re.search(r"(?i)(_DRAFT|FINAL_v\d+|REPLY|MOTION|BRIEF|PETITION|CLAIM|NOTICE)", f.name):
            continue
        # collapse version families: keep only the newest of each stem family
        fam = re.sub(r"_v\d+.*$", "", f.stem)
        if fam in seen_kind:
            continue
        seen_kind.add(fam)
        head = _read(f, 1500)
        status = (re.search(r"(?i)status:\s*(.+)", head) or [None, "?"])[1]
        inst.append(f"- {f.name} — status: {str(status).strip()[:80]}")
    ob = ROOT / "efiling" / "letterstream_outbox"
    for j in ob.glob(f"*{slug.split(':')[-1][:12]}*/**/job.json"):
        d = json.loads(_read(j) or "{}")
        rcpts = d.get("recipients") or [{}]
        inst.append(f"- OUTBOX: {d.get('filing', '?')} -> {rcpts[0].get('name', '?')} "
                    f"(filed={d.get('mail', {}).get('filed')})")
    parts += ["## 4. INSTRUMENTS ALREADY IN PLAY (posture — not a blank slate)", ""] + (inst[:30] or ["(none)"]) + [""]

    # 5. DOCTRINE — analysis notes wikilinked from the case, or naming its terms
    links = _wikilinks(cf_text)
    doct: list[str] = []
    for f in sorted(ANALYSIS.glob("*.md")):
        t = _read(f, 40000)
        linked = f.stem in links
        named = any(k in t.lower() for k in key_terms if len(k) > 6)
        if linked or named:
            first = next((l for l in t.splitlines() if l.startswith("#")), f.stem)
            doct.append(f"- [[analysis/{f.stem}]] — {first.lstrip('# ')[:100]}{' (WIKILINKED)' if linked else ''}")
    parts += ["## 5. GOVERNING DOCTRINE NOTES (read before drafting — the deep layer)", ""] + (doct[:20] or ["(none)"]) + [""]

    # 6. APPEAL SCOPE — verbatim from every notice of appeal in intake
    scopes: list[str] = []
    for f in sorted(case_dir.glob("intake/*.txt")):
        t = _read(f, 30000)
        if APPEAL_PAT.search(t):
            m = re.search(r"(?is)NOTICE OF APPEAL.{0,1200}", t)
            if m:
                scopes.append(f"### {f.name}\n```\n{m.group(0)[:1200]}\n```")
    parts += ["## 6. APPEAL SCOPE (verbatim — downstream filings must NEVER narrow this)", ""] + (scopes or ["(no notice of appeal in intake)"]) + [""]

    out = case_dir / "MATTER_BRIEF.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"[matter_brief] {out} ({len(parts)} blocks: locked={len(locked)}, "
          f"instruments={len(inst)}, doctrine={len(doct)}, appeal_scopes={len(scopes)})")
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "fed_26-cv-00123")
