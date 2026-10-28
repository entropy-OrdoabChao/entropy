#!/usr/bin/env python3
"""rule_requirements.py -- read a court's OWN rules from corpus, derive the mandatory
filing contract, and WIRE it: profile DATA + stub + routing report.

USER-DIRECTED 2026-09-27: "there is one thing hermes and the m0 need to be able to do
when rules like this happen and we don't have a stub it should read the requirements and
create the stub and look for the script where it needs to be routed and update. sandbox
first then apply. the goal is automation."

The defect this closes, concretely: Phila.Civ.R. *210 mandates five separately and
distinctly titled brief sections in a fixed order. m1_finisher.anatomy_sections() took a
`court` argument and IGNORED it, so every Court of Common Pleas brief this pipeline ever
produced carried the FEDERAL anatomy (INTRODUCTION / STATEMENT OF THE CASE / STANDARD OF
REVIEW) -- a rule violation in the forum it was filed in. Nobody noticed because no organ
ever read the court's own rules.

Design per the user's locked quality doctrine:
  * LLM = COMPREHENSION. Deriving "which sections does this rule mandate, in the rule's
    own order" requires reading the rule. A keyword table faking that comprehension is
    rejected on sight.
  * Deterministic = GUARDRAIL. Every derived section must be ANCHORED in the rule's
    verbatim text (its `requires` clause must appear, near-verbatim, in the rule) or it is
    REJECTED. The LLM selects; the corpus text bounds the choice-space. No invented
    sections, ever -- a hallucinated "mandatory section" would put a heading in a filed
    document that no rule supports.

Corpus probe discipline (legal-corpus-ingest lesson, learned the slow way): FTS phrase
MATCH is the arbiter. An unanchored LIKE '%...%' over chunk_text scans 3.5M rows and has
to be killed -- it manufactures false "NOT IN CORPUS" verdicts.

CLI
    python3 -m pipeline.rule_requirements --court PA_CP_PHILA --kind brief_or_opposition \
        --probe 'separately and distinctly titled' --apply
    python3 -m pipeline.rule_requirements --court PA_CP_PHILA --kind brief_or_opposition \
        --case example_v_insurer --filing-type opposition_prelim_objections \
        --make-stub --apply

Without --apply nothing is written: the derived contract is printed for review.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

def _find_base() -> Path:
    """The repo root: the dir that actually HOLDS `pipeline/`.

    `__file__.parent.parent` is wrong the moment this module is exercised from anywhere but
    `pipeline/` -- from the sandbox root it climbed to `_sandbox_ruleauto/`, so every routing
    target read MISSING and the report claimed no consumer was wired when all three exist.
    Walk up to the real marker, then honour an explicit override.
    """
    env = os.environ.get("BEEZLE_BASE")
    if env and (Path(env) / "pipeline").is_dir():
        return Path(env)
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "pipeline").is_dir():
            return cand
    return here.parent.parent


BASE = _find_base()
PROFILES = BASE / "output" / "audit" / "m1_style_profiles.json"
CASES = BASE / "obsidian_sync" / "cases"
BACKUPS = BASE / ".backups"
CHUNK_DB = Path(os.environ.get("BEEZLE_CHUNK_DB",
                               str(_eroot() / "corpus/chunk_text.db")))

# Scripts that CONSUME the filing contract. The routing report checks each one actually
# reads the profile, so a future organ cannot silently ignore a court's rules the way
# anatomy_sections() ignored its `court` argument for the pipeline's whole life.
ROUTING_TARGETS = (
    ("pipeline/m1_finisher.py", "_mandatory_heads", "section order + mandatory headings"),
    ("pipeline/m1_rebuttal.py", "_style_contract", "voice-pass style contract"),
    # Marker must be a symbol the consumer ACTUALLY defines. `required_blocks` was a guess and
    # exists nowhere in m1_filing_gate.py (it reads the contract via `_mandatory_sections`), so
    # the report said NOT_WIRED for a consumer that reads the profile at line 45 -- a false
    # defect manufactured by the auditor rather than found in the code.
    ("pipeline/m1_filing_gate.py", "_mandatory_sections", "pre-mail compliance gate"),
)


# ---------------------------------------------------------------- corpus

def rule_text_from_corpus(probes, cap: int = 4, prefer: str = "") -> list:
    """Pull the court's own rule text by FTS phrase match.

    `probes` are 4-8 word EXACT phrases from the rule. Returns
    [{"chunk_id","source","jurisdiction","text"}, ...] -- deduped by chunk_id, order of
    probe priority preserved.
    """
    if not CHUNK_DB.is_file():
        return []
    out, seen = [], set()
    db = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
    try:
        cur = db.cursor()
        for ph in probes:
            ph = (ph or "").strip().strip('"')
            if not ph:
                continue
            try:
                # `source_id` IS the chunk hash that chunk_text.chunk_id holds; the FTS table's
                # own `chunk_id` column does NOT exist (schema: text, source_id UNINDEXED).
                # Selecting a non-existent column raised OperationalError, the bare `except
                # sqlite3.Error: continue` swallowed it, and every probe returned zero -- a
                # FALSE "NOT IN CORPUS" on a rule that is in corpus twice. Absence of a hit is
                # never a finding until the query itself is proven to run.
                cur.execute(
                    "SELECT source_id FROM chunk_text_fts "
                    "WHERE chunk_text_fts MATCH ? LIMIT ?", (f'"{ph}"', cap))
                rows = cur.fetchall()
            except sqlite3.Error as e:
                print(f"rule_requirements: FTS probe {ph!r} FAILED: {e}", file=sys.stderr)
                continue
            for (cid,) in rows:
                if cid in seen:
                    continue
                cur.execute("SELECT source, jurisdiction, text FROM chunk_text "
                            "WHERE chunk_id = ?", (cid,))
                r = cur.fetchone()
                if not r:
                    continue
                seen.add(cid)
                out.append({"chunk_id": cid, "source": r[0] or f"chunk:{cid}", "jurisdiction": r[1] or "",
                            "text": " ".join((r[2] or "").split())})
    finally:
        db.close()
    if prefer:
        # A rule phrase legitimately appears in many documents (10 chunks carry "separately and
        # distinctly titled"; only two are the Philadelphia compiled rules). Deriving the filing
        # contract from a chunk of some OTHER court's rules would encode the wrong court's
        # anatomy, so hoist the requested rule FAMILY to the front by source key. Kept as
        # ordering, never filtering: the other hits stay available as corroboration.
        pl = prefer.lower()
        out.sort(key=lambda h: 0 if pl in (h.get("source") or "").lower() else 1)
    return out


def _rule_window(text: str, anchor: str, before: int = 400, after: int = 3200) -> str:
    """The slice of a chunk around the rule we care about -- a compiled-rules chunk holds
    many rules, and feeding all of them invites the model to blend two rules' sections."""
    i = text.lower().find((anchor or "").lower())
    if i < 0:
        return text[:after]
    return text[max(0, i - before): i + after]


# ---------------------------------------------------------------- LLM lane

def _complete(prompt: str, max_tokens: int = 1400, timeout: int = 300) -> str:
    """The proven m0 lane (case_facts_refresh._call_direct). Never a second endpoint."""
    sys.path.insert(0, str(BASE))
    from pipeline.case_facts_refresh import _call_direct  # noqa: E402
    return _call_direct(prompt, max_tokens=max_tokens, timeout=timeout) or ""


_DERIVE_SYS = """You are reading a COURT RULE verbatim. Extract the document sections the
rule MANDATES for the filing named, in the rule's OWN order.

Return STRICT JSON, no prose, no markdown fence:
{"sections":[{"heading":"...","rule":"...","requires":"..."}],
 "attachments":[{"item":"...","label":"...","rule":"...","requires":"..."}],
 "answer_required":{"rule":"...","required_for":"...","not_required_for":"...","requires":"..."},
 "endorsement":{"rule":"...","requires":"..."},
 "citation_rules":["..."]}

Hard constraints:
- "heading" = the section's name as a filer would title it, ALL CAPS.
- "rule" = the subdivision that mandates it, exactly as the text numbers it (e.g. *210(2)).
- "requires" = what the rule demands, quoted or closely paraphrased FROM THE TEXT GIVEN.
- Include a section ONLY if the text given actually mandates it. Do not add conventional
  sections (no INTRODUCTION, no STANDARD OF REVIEW) unless this rule names them.
- "citation_rules" = any rule about HOW authority must be cited in the filing.
- "attachments" = every document the rule says must be FILED TOGETHER WITH / ATTACHED TO
  this filing (briefs, exhibits, copies of pleadings, proposed orders, certificates,
  verifications). "item" = the rule's own description of the document, ALL CAPS;
  "label" = the 1-3 word title a filer puts on that document, ALL CAPS (the word the rule
  itself uses for the thing when marked/filed -- e.g. a rule saying "marked as exhibits"
  makes the label EXHIBITS; "a proposed order" makes PROPOSED ORDER; "a brief" makes
  BRIEF); "requires" =
  the rule's words, including any condition on the item (e.g. "shall contain no reference
  to the attorney proposing same"). Omit if the text mandates none.
- "answer_required" = ONLY if the text says when a responsive filing IS or IS NOT required
  (e.g. "an answer ... is required only to ..."; "need not be filed to ..."). Quote the
  grounds. Omit entirely if the text is silent.
- If the text mandates nothing, return {"sections":[]}.
"""


def _json_from(txt: str) -> dict:
    """Parse the model's JSON, tolerating a fence or leading prose."""
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return {}


# ---------------------------------------------------------------- guardrail

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def _anchored(claim: str, rule_text: str, min_run: int = 4) -> bool:
    """Is `claim` traceable to the rule's verbatim text?

    Deterministic guardrail on an LLM derivation: require a run of >= min_run consecutive
    content words from the claim to appear in the rule text. A hallucinated section cannot
    satisfy this, because its wording exists nowhere in the rule.
    """
    stop = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "shall", "be", "is",
            "as", "it", "that", "which", "this", "each", "any", "all", "on", "by", "with"}

    def _content(s: str):
        return [w for w in _norm(s).split() if w not in stop and len(w) > 2]

    words = _content(claim)
    # Strip stopwords from BOTH sides. Filtering only the claim and then searching the RAW
    # rule text can never match: "State the particular pleading ... before the court" reduces
    # to `state particular pleading before court`, a string absent from the rule because the
    # rule still carries its articles. Measured cost of the asymmetry: 0/4 real *210 sections
    # anchored -- the guardrail silently rejected every genuine section while still rejecting
    # hallucinations, i.e. it looked like it worked and would have emptied the contract.
    hay = " " + " ".join(_content(rule_text)) + " "
    if not words:
        return False
    if len(words) < min_run:
        return f" {' '.join(words)} " in hay
    for k in range(len(words) - min_run + 1):
        if f" {' '.join(words[k:k + min_run])} " in hay:
            return True
    return False


# RULE-LABEL GUARDRAIL (2026-10-20, S-rulereq-label-anchor). The item/requirement text was anchored in the rule, but
# the RULE NUMBER the model attached to it was not: on N.J. R. 4:5-1(b) it derived the Case Information Statement and
# the other-actions certification correctly and labelled both "R. 4:6-2(a)/(b)" -- a cite to a rule that says nothing
# of the kind, which would print on a filed checklist. The number is a FACT of the source chunk, not a comprehension
# call: read it from the chunk's own header ("Rule 4:5-1", "Rule *1028") and overwrite any label whose number differs.
_RULE_NO = r"\*?\d+(?::\d+[A-Za-z]?-\d+[A-Za-z]?(?:\.\d+)?)?"


def _rule_id_of(chunk_text: str) -> str:
    """The rule number a compiled-rules chunk declares in its own header ('' when it declares none)."""
    m = re.search(r"\bRule\s+(" + _RULE_NO + r")", (chunk_text or "")[:400])
    return m.group(1) if m else ""


def _fix_rule_label(rule: str, rule_id: str, rule_text: str):
    """(label, relabelled?) -- keep the model's label only when its number is the source rule's number (or, when the
    source declares no number, when that number appears in the rule text). Otherwise rewrite it to the source number."""
    if not rule:
        return rule, False
    cores = [c for c in re.findall(_RULE_NO, rule) if ":" in c or len(c.lstrip("*")) >= 3]
    if not cores:
        return rule, False
    core = cores[0]
    if rule_id:
        if core == rule_id:
            return rule, False
        return rule[:rule.find(core)] + rule_id, True
    return (rule, False) if core in rule_text else ("", True)


def derive_requirements(rule_text: str, court: str, kind: str,
                        filing_desc: str = "", rule_id: str = "") -> dict:
    """LLM derives; the rule's own text vetoes. Returns the vetted contract."""
    prompt = (f"{_DERIVE_SYS}\n\nCOURT: {court}\nFILING: {filing_desc or kind}\n\n"
              f"RULE TEXT (verbatim from corpus):\n{rule_text}\n\n"
              f"Extract the mandated sections now.")
    raw = _complete(prompt)
    got = _json_from(raw)
    secs_in = got.get("sections") or []
    kept, rejected, relabeled = [], [], []
    for s in secs_in:
        if not isinstance(s, dict):
            continue
        head = re.sub(r"\s+", " ", str(s.get("heading") or "")).strip().upper()
        req = re.sub(r"\s+", " ", str(s.get("requires") or "")).strip()
        rule = re.sub(r"\s+", " ", str(s.get("rule") or "")).strip()
        if not head:
            continue
        # GUARDRAIL: the heading OR its requirement must be anchored in the rule text.
        rule, _rl = _fix_rule_label(rule, rule_id, rule_text)
        if _rl: relabeled.append(head)
        if _anchored(head, rule_text, 2) or _anchored(req, rule_text, 4):
            kept.append({"heading": head, "rule": rule, "requires": req})
        else:
            rejected.append(head)
    endo = got.get("endorsement") or {}
    if endo and not _anchored(str(endo.get("requires") or ""), rule_text, 4):
        endo = {}
    cites = [c for c in (got.get("citation_rules") or [])
             if isinstance(c, str) and _anchored(c, rule_text, 4)]
    # ATTACHMENTS -- same guardrail as sections: an item the rule's text does not name is
    # rejected. (2026-09-27: Phila.Civ.R. *1028(c)(4)(b)-(c) mandate exhibits INCLUDING
    # the complaint and a proposed order; the schema had no slot, so the organ read the
    # rule and dropped half of it, and the pipeline shipped briefs with no exhibits.)
    atts, att_rej = [], []
    for a in (got.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        item = re.sub(r"\s+", " ", str(a.get("item") or "")).strip().upper()
        label = re.sub(r"\s+", " ", str(a.get("label") or "")).strip().upper()
        req = re.sub(r"\s+", " ", str(a.get("requires") or "")).strip()
        rule = re.sub(r"\s+", " ", str(a.get("rule") or "")).strip()
        if not item:
            continue
        # the label must be a word the rule text actually uses (singular/plural tolerated)
        _lab_ok = bool(label) and all(
            re.search(r"\b" + re.escape(w.rstrip("S")) + r"S?\b", rule_text, re.I)
            for w in label.split())
        rule, _rl = _fix_rule_label(rule, rule_id, rule_text)
        if _rl: relabeled.append(item)
        if _anchored(item, rule_text, 2) or _anchored(req, rule_text, 4):
            atts.append({"item": item, "label": label if _lab_ok else "",
                         "rule": rule, "requires": req})
        else:
            att_rej.append(item)
    ans = got.get("answer_required") or {}
    if ans and not isinstance(ans, dict):
        ans = {}
    if ans and not _anchored(str(ans.get("requires") or ""), rule_text, 4):
        ans = {}
    return {"sections": kept, "rejected": rejected, "endorsement": endo,
            "citation_rules": cites, "attachments": atts, "attachments_rejected": att_rej,
            "answer_required": ans, "relabeled": relabeled, "rule_id": rule_id, "raw_len": len(raw)}


# ---------------------------------------------------------------- profile write

def upsert_profile(court: str, kind: str, reqs: dict, authority: str,
                   sources: list, apply: bool = False) -> dict:
    """Write mandatory_sections into m1_style_profiles.json -- DATA, never code.

    MERGE, never replace. One filing is governed by SEVERAL rules (Phila: *210 gives the
    brief's five sections; *1028(c) gives what must be filed WITH it). Each derivation run
    reads one rule window; if the run overwrote the entry, whichever rule ran last would
    erase the other (2026-09-27: the *1028(c) run wiped the *210 sections and the *210
    contract had to be hand-restored). Sections/attachments are keyed by (rule, heading);
    the incoming run wins on a key collision, everything else is kept.
    """
    prof = json.loads(PROFILES.read_text(encoding="utf-8")) if PROFILES.is_file() else {}
    entry = dict((prof.get(court) or {}).get(kind) or {})
    prev_auth = str(entry.get("_authority") or "")
    entry["_authority"] = (authority if not prev_auth or authority in prev_auth
                           else f"{prev_auth}; {authority}")
    prev_derived = entry.get("_derived") or {}
    prev_sources = list(prev_derived.get("sources") or [])
    entry["_derived"] = {"organ": "rule_requirements", "at": datetime.now().isoformat(
        timespec="seconds"),
        "sources": list(dict.fromkeys(prev_sources + list(sources)))[:8]}

    def _merge(old: list, new: list, key) -> list:
        merged = {key(x): x for x in (old or []) if isinstance(x, dict)}
        for x in (new or []):
            if isinstance(x, dict):
                merged[key(x)] = x
        return list(merged.values())

    def _skey(s):
        return (str(s.get("rule") or "").strip(), str(s.get("heading") or "").strip().upper())

    def _akey(a):
        return (str(a.get("rule") or "").strip(), str(a.get("item") or "").strip().upper())

    # A derived section that is itself an ATTACHMENT-level item ("BRIEF OR MEMORANDUM OF LAW"
    # from *1028(c)(4)(a)) must not displace the brief's own internal sections from *210.
    # Sections whose heading equals an attachment item are demoted to attachments.
    att_items = {str(a.get("item") or "").strip().upper() for a in (reqs.get("attachments") or [])}
    new_secs, demoted = [], []
    for s in reqs["sections"]:
        h = str(s.get("heading") or "").strip().upper()
        if h in att_items or re.fullmatch(r"(BRIEF|MEMORANDUM)( OR MEMORANDUM)?( OF LAW)?", h):
            demoted.append({"item": h, "rule": s.get("rule") or "", "requires": s.get("requires") or ""})
        else:
            new_secs.append(s)
    entry["mandatory_sections"] = _merge(entry.get("mandatory_sections"), new_secs, _skey)
    atts_in = (reqs.get("attachments") or []) + demoted
    if atts_in:
        entry["mandatory_attachments"] = _merge(entry.get("mandatory_attachments"), atts_in, _akey)
    if reqs.get("answer_required"):
        entry["answer_required"] = reqs["answer_required"]
    if reqs.get("endorsement"):
        entry["endorsement"] = reqs["endorsement"]
    if reqs.get("citation_rules"):
        cc = dict(entry.get("citation_conventions") or {})
        cc["rule_derived"] = list(dict.fromkeys(
            list(cc.get("rule_derived") or []) + list(reqs["citation_rules"])))
        entry["citation_conventions"] = cc
    order = [f"{s['heading']} ({s['rule']})" if s.get("rule") else s["heading"]
             for s in entry["mandatory_sections"]]
    if order:
        entry["section_order"] = (["CAPTION", "TITLE"] + order
                                  + ["SIGNATURE BLOCK", "CERTIFICATE OF SERVICE"])
    if not apply:
        return {"written": False, "entry": entry}
    prof.setdefault(court, {})[kind] = entry
    BACKUPS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if PROFILES.is_file():
        (BACKUPS / f"m1_style_profiles.json.pre_ruleauto_{ts}.bak").write_text(
            PROFILES.read_text(encoding="utf-8"), encoding="utf-8")
    PROFILES.write_text(json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"written": True, "entry": entry,
            "backup": f".backups/m1_style_profiles.json.pre_ruleauto_{ts}.bak"}


# ---------------------------------------------------------------- stub

_STUB_TMPL = """# {title}

**Court:** {court}
**Filing type:** {filing_type}
**Generated by:** pipeline/rule_requirements.py from the court's own rules ({at})

## Primary authority

{authority}

## Claim taxonomy

- (m0 fills from the case's audit stream)

## CONTROLLING AUTHORITY (m0-verified — weave per count)

- (m0 fills from m0_authority_spine; every cite carries its holding inline)

## WHAT THE MOVANT FILED (record scope — verify before drafting)

- (m0 fills from intake: which counts were attacked, under which rules, what relief)

## THE PREMISE TO ATTACK FIRST (single point of failure in their brief)

- (m0 fills)

## SECTION MAP (rule-mandated — every section below is required by the court's own rules)

{section_map}

## RULE COMPLIANCE (mandatory — this filing is defective without these)

{compliance}

## Drafting rules (locked)

- Every section above is MANDATORY and must be separately and distinctly titled.
- Keep the rule's ORDER; a section out of order is a defect on the face of the filing.
- {cite_rule}
- Zero-waiver: every argument heading in the movant's brief gets an answering section.
- No authority without its holding or the proposition it stands for, stated inline.
"""


def stub_path_for(case: str, filing_type: str) -> Path:
    """Where m1 looks for the REAL stub (never the router in the case root)."""
    root = CASES / case / "drafts" / "_bundle11_packet" / "motions"
    chains = sorted([p for p in root.glob("chain_*") if p.is_dir()]) if root.is_dir() else []
    target = chains[0] if chains else root
    return target / f"{filing_type}.md"


def _equivalent_stub(case: str, reqs: dict, exclude: Path = None) -> Path:
    """An existing stub in this chain that ALREADY encodes this rule contract, if any.

    Without this the organ writes a TWIN: a numbered `07_brief_opposition_prelim_objections.md`
    already carried the identical five *210 section headings, and a second run produced
    `brief_opposition_preliminary_objections.md` differing only in its title line. Two stubs for
    one filing is worse than none -- m1 picks one and a human cannot tell which is authoritative.
    Match on the RULE SUBDIVISIONS present (the contract), never on the filename.
    """
    heads = [s.get("rule", "") for s in (reqs.get("sections") or []) if s.get("rule")]
    if not heads:
        return None
    root = CASES / case / "drafts" / "_bundle11_packet" / "motions"
    if not root.is_dir():
        return None
    for md in sorted(root.rglob("*.md")):
        if exclude and md.resolve() == Path(exclude).resolve():
            continue
        if md.name.endswith(("_DRAFT.md", "_M1_DRAFT.md", "_PROOF_SHEET.md")):
            continue
        try:
            body = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if all(h in body for h in heads):
            return md
    return None


def generate_stub(case: str, court: str, filing_type: str, reqs: dict,
                  authority: str, title: str = "", apply: bool = False) -> dict:
    """Create the m0 stub for a filing type the rules require and we have none for."""
    path = stub_path_for(case, filing_type)
    dup = _equivalent_stub(case, reqs, exclude=path)
    if dup is not None:
        return {"written": False, "path": str(dup), "existing": True,
                "why": f"stub already encodes {[s.get('rule') for s in reqs['sections']]}"}
    smap = []
    for i, s in enumerate(reqs["sections"], 1):
        letter = chr(ord("A") + i - 1)
        smap.append(f"### {letter}. {s['heading']} — required by {s['rule'] or 'rule'}\n\n"
                    f"- {s['requires']}\n")
    comp = [f"- **{s['rule'] or 'rule'}** — {s['heading']}: {s['requires']}"
            for s in reqs["sections"]]
    if reqs.get("endorsement"):
        e = reqs["endorsement"]
        comp.append(f"- **{e.get('rule', 'endorsement')}** — ENDORSEMENT: "
                    f"{e.get('requires', '')}")
    cite_rule = (reqs.get("citation_rules") or
                 ["Cite authority with its holding, never for general reference."])[0]
    body = _STUB_TMPL.format(
        title=title or filing_type.replace("_", " ").title(),
        court=court, filing_type=filing_type,
        at=datetime.now().strftime("%Y-%m-%d"),
        authority=authority,
        section_map="\n".join(smap) or "- (none derived)",
        compliance="\n".join(comp) or "- (none derived)",
        cite_rule=cite_rule)
    if not apply:
        return {"written": False, "path": str(path), "preview": body[:900]}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (BACKUPS / f"{path.name}.pre_ruleauto_{ts}.bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(body, encoding="utf-8")
    return {"written": True, "path": str(path), "bytes": len(body)}


# ---------------------------------------------------------------- routing

def routing_report(court: str, kind: str) -> list:
    """Which scripts consume this contract, and do they actually read it?

    The failure this detects is the one that produced the *210 defect: a consumer that
    accepts `court` but never looks the profile up. Reported per target so a human (or the
    next run) can see exactly where a rule would be ignored.
    """
    rows = []
    for rel, marker, role in ROUTING_TARGETS:
        p = BASE / rel
        if not p.is_file():
            rows.append({"script": rel, "role": role, "status": "MISSING"})
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        reads_profile = ("m1_style_profiles" in src) or ("_style_profile" in src)
        has_marker = marker in src
        rows.append({
            "script": rel, "role": role,
            "status": ("WIRED" if (reads_profile and has_marker)
                       else "READS_PROFILE_NO_MARKER" if reads_profile
                       else "NOT_WIRED"),
            "reads_profile": reads_profile, "marker": has_marker})
    return rows


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--court", required=True)
    ap.add_argument("--kind", default="brief_or_opposition")
    ap.add_argument("--probe", action="append", default=[],
                    help="exact rule phrase (repeatable); FTS arbiter")
    ap.add_argument("--prefer-source", default="",
                    help="source-key substring of the governing rule family, e.g. "
                         "'cp-trial-civil' -- hoists that court's own rules above "
                         "same-phrase hits in unrelated documents")
    ap.add_argument("--anchor", default="",
                    help="rule name to window the chunk on, e.g. 'Rule *210'")
    ap.add_argument("--filing-desc", default="")
    ap.add_argument("--case", default="")
    ap.add_argument("--filing-type", default="")
    ap.add_argument("--make-stub", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    probes = a.probe or ["separately and distinctly titled"]
    hits = rule_text_from_corpus(probes, prefer=a.prefer_source)
    if not hits:
        print(f"rule_requirements: NO corpus hit for {probes} — pull + ingest the rule "
              f"first (legal-corpus-ingest), never guess the contract")
        return 2
    print(f"[corpus] {len(hits)} chunk(s): "
          f"{', '.join(sorted({h['source'] for h in hits}))}")
    window = _rule_window(hits[0]["text"], a.anchor or probes[0])
    print(f"[corpus] rule window {len(window)} chars from {hits[0]['source']}")

    reqs = derive_requirements(window, a.court, a.kind, a.filing_desc, rule_id=_rule_id_of(hits[0]["text"]))
    if reqs.get("relabeled"):
        print(f"[derive] rule label rewritten to the source rule ({reqs['rule_id'] or 'none declared'}) on "
              f"{len(reqs['relabeled'])} item(s): {reqs['relabeled']}")
    print(f"[derive] kept {len(reqs['sections'])} section(s); "
          f"rejected {len(reqs['rejected'])} unanchored: {reqs['rejected']}")
    for s in reqs["sections"]:
        print(f"    {s['rule'] or '-':10} {s['heading']}")
    for at in reqs.get("attachments") or []:
        print(f"    ATTACH {at['rule'] or '-':14} {at['item']}")
    if reqs.get("attachments_rejected"):
        print(f"[derive] rejected {len(reqs['attachments_rejected'])} unanchored attachment(s): "
              f"{reqs['attachments_rejected']}")
    if reqs.get("answer_required"):
        ar = reqs["answer_required"]
        print(f"    ANSWER {ar.get('rule') or '-':14} required_for={ar.get('required_for')!r} "
              f"not_required_for={ar.get('not_required_for')!r}")
    if not reqs["sections"] and not reqs.get("attachments"):
        print("rule_requirements: derived NOTHING — refusing to write an empty contract")
        return 3

    authority = (f"Derived from {hits[0]['source']} (corpus) by rule_requirements; "
                 f"probes={probes}")
    pr = upsert_profile(a.court, a.kind, reqs, authority,
                        sorted({h["source"] for h in hits}), apply=a.apply)
    print(f"[profile] {'WROTE' if pr['written'] else 'DRY-RUN'} {a.court}.{a.kind}"
          + (f" (backup {pr['backup']})" if pr.get("backup") else ""))

    if a.make_stub:
        if not (a.case and a.filing_type):
            print("[stub] --make-stub needs --case and --filing-type")
        else:
            st = generate_stub(a.case, a.court, a.filing_type, reqs, authority,
                               apply=a.apply)
            if st.get("existing"):
                print(f"[stub] EXISTS — reusing {st['path']}")
                print(f"        {st['why']}")
            else:
                print(f"[stub] {'WROTE' if st['written'] else 'DRY-RUN'} {st['path']}")

    print("[routing]")
    for r in routing_report(a.court, a.kind):
        print(f"    {r['status']:24} {r['script']}  ({r['role']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
