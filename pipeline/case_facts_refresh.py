#!/usr/bin/env python3
"""case_facts_refresh — the "OCR updates case fact" step, run right after the email pull.

For each active case whose intake is NEWER than CASE_FACTS.md:
  1. ecf_resolver.tie_docket  — DETERMINISTIC: recover each new filing's ECF# from the CM/ECF
     "Document N Filed" header and drop {docket,date_filed,type,filename} into CASE_TIMELINE.json.
  2. Hermes reasoning (legal-pipeline-operations skill) — read the new filing, identify opposing
     counsel/judge + posture, and update CASE_FACTS.md (PENDING MOTIONS, DOCKET HIGHLIGHTS,
     DEFENDANT POSTURES, the rebuttal m0 must make).
Then m0 reads the current CASE_FACTS + CASE_TIMELINE and drafts the reply to the latest filing.

Split by design (R7): the LLM reasons the facts/strategy; ecf_resolver ties the structured docket
deterministically. Neither fabricates — tie_docket skips any file with no recoverable ECF#.

CLI:
  python -m pipeline.case_facts_refresh                  # all cases with intake newer than CASE_FACTS
  python -m pipeline.case_facts_refresh fed_26-cv-00123  # one case (forced)
  python -m pipeline.case_facts_refresh --no-hermes      # tie_docket only (deterministic, no LLM)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys
import os
import re
import subprocess
from pathlib import Path
from pipeline._sentsplit import split_sentences as _split_sents

ROOT = _eroot()
CASES = ROOT / "obsidian_sync" / "cases"
HERMES = str(_ehermes() / "hermes-agent/venv/bin/hermes")
# LANE LOCK: local-gpu is the CHAT + bots lane; m0 analysis runs on the local-gpu (qwen38-local-gpu ->
# :18020, the same endpoint _call_direct already uses). These defaults previously named
# local-gpu/local-model-heretic, which was wrong twice over: wrong lane (analysis would starve the
# user's chat under BEEZLE_CF_AGENTIC=1) and a model local-gpu no longer serves at all -- it
# serves qwen3.8-27b-exl3 now, so the agentic path would have requested a nonexistent
# model. Naming the real lane makes both paths agree instead of leaning on the runtime
# "not served by this lane" rescue.
# Deliberate fallback if the local-gpu is down (borrows the chat lane -- expect chat to slow):
#   BEEZLE_CF_PROVIDER=local-gpu BEEZLE_CF_MODEL=qwen3.8-27b-exl3
# NOTE: providers `manifest` / `manifest-fast` both point at :8000, which is NOT running
# (AGENTS.md sec 10 lists it as always-up; it is not). Do not use them as a fallback
# without first confirming :8000 is live.
PROVIDER = os.environ.get("BEEZLE_CF_PROVIDER", "qwen38-local-gpu")
MODEL = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b")
SKILL = "legal-pipeline-operations"
HERMES_TIMEOUT = 1800
SECTION_TIMEOUT = 700       # bounded per section -- no single giant call hangs the lane
# connected_notes loads BAAI/bge-base-en-v1.5 and embeds newly-changed notes across a
# 163k-file vault. It IS incremental (cache keyed by slug=sha1(content)), but the cold
# model load plus one batch overran the old 300s ceiling, so the graph-linking step was
# silently skipped -- leaving new notes unlinked, which is exactly the index rot that
# makes "is this already fixed?" unanswerable. Give it room; it still fails soft.
CONNECT_TIMEOUT = int(os.environ.get("BEEZLE_CONNECT_TIMEOUT", "900"))
PYBIN = str(_ehermes() / "hermes-agent/venv/bin/python3")

# Reliable-write contract: each section OWNS one canonical top-level markdown block. The agent READS +
# grounds, then RETURNS only that block (it does NOT write the file); Python upserts it deterministically
# and verifies it landed. This fixes the false-"ok" where the CLI exited 0 but no edit was persisted.
_SECTION_HEADERS = {
    "latest_order": "## LATEST COURT ORDER",
    "parties":      "## DEFENDANT POSTURES",
    "motion":       "## PENDING MOTIONS",
    "rebuttal":     "## ADVERSE-FILING DEFECTS & LEVERS",
    "disposition":  "## APPEAL & DISPOSITION SCOPE",
    "order_errors": "## ADVERSE-ORDER ERRORS (court's ignored rules)",
}
# ---------------------------------------------------------------------------
# FOLD SECTIONS -- the contract m0 GUARANTEES it writes into the analysis and
# m1 consumes by name. m0 is the producer, so the list lives HERE; m1 imports
# it. Duplicating it in the drafter let the two drift silently.
# Each entry: (h3 heading, our proposition[, heading suffix]).
# ---------------------------------------------------------------------------
FOLD_SECTIONS = (
    ("WHEN THE BREACH BEGAN",
     "The breach began when the insured interest ended"),
    ("DISTINCTIONS ON THIS RECORD",
     "The movant's authorities do not reach this record"),
    ("WHAT THEIR AUTHORITIES ACTUALLY SAY",
     "The movant's characterizations do not match the opinions",
     " — the movant's description checked against the opinion"),
    ("NON-BINDING AUTHORITY THE MOVANT RELIES ON",
     "The movant's non-binding authority"),
    ("NON-RESPONSIVE OBJECTION",
     "The objection does not answer the count as pleaded"),
    ("RECORD FACTS ANSWERING THE OBJECTIONS",
     "The record answers the objection"),
    # m0 walks every pleaded count element-by-element against the record. Without this
    # entry the whole fold was computed and then dropped on the floor by m1.
    ("AM JUR ELEMENT CHAINS",
     "Every element of the count is met on the face of the pleading"),
    # A defect in the MOVANT'S OWN FILING under the court's local rules is an
    # independent, threshold ground -- it is decided before the merits and the rule
    # usually carries its own sanction (e.g. Phila.Civ.R. *1028(c): the court may decide
    # preliminary objections against a party who fails to attach the items necessary to
    # determine them). m0 records these per case; without this entry they never reach
    # the draft.
    ("RULE DEFECTS IN THE MOVANT'S FILING",
     "The movant's own filing does not comply with the rules governing it"),
    # The movant's OWN participation in the authorities it cites (its firm or counsel
    # appeared and lost; it was itself the losing party). Computed by the provenance fold
    # from the full opinion text; without this entry it sat in CASE_FACTS and never
    # reached the draft (2026-09-27 run 8c).
    ("PROVENANCE OF THE MOVANT'S AUTHORITIES",
     "The movant's own history with the authorities it cites"),
    # Binding later authority that construes/limits a case the movant cites, which the
    # movant did not cite (Pa.R.P.C. 3.3(a)(2)). Found in corpus by who-cites-whom.
    ("OMITTED CONTROLLING AUTHORITY",
     "Binding authority on the movant's own cited cases that the movant omitted"),
)

_OUTPUT_CONTRACT = (
    "\n\n=== OUTPUT CONTRACT (this OVERRIDES any earlier instruction to update/write/preserve a file) ===\n"
    "Do NOT write, edit, or create any file. You MAY read files and call search_chunks to ground yourself, "
    "but your RESPONSE must be ONLY the finished GitHub-markdown block for this section, beginning with the "
    "EXACT line:\n{header}\n"
    "and containing the full grounded, cited analysis beneath it. No preamble, no closing remarks, no "
    "<think> tags, no tool narration -- output the block and nothing else. The pipeline writes it into "
    "CASE_FACTS.md deterministically and verifies it landed."
)


class CorpusLocked(RuntimeError):
    """The corpus DB could not be read (locked/unavailable). NEVER absence.

    A reader that swallows this and returns "" turns a cron writer's lock window into a
    false "NOT IN CORPUS" on the record (Wenk 228 A.3d 540, 2026-09-27: 37 chunks in
    corpus, marked NOT-VERIFIED because rechunk_backfill held the lock). Callers re-raise.
    """


_CORPUS_DB = "file:~/turbovec/chunk_text.db?mode=ro"


def _corpus_connect(timeout: float = 90.0):
    """Read-only corpus connection that WAITS on a writer instead of failing.

    `timeout` is sqlite's busy handler: it retries for that long before raising. The
    rechunk writer holds each transaction for milliseconds, so 90s covers any window.
    A failure after that is raised as CorpusLocked so the fold marks the corpus
    UNAVAILABLE rather than the authority ABSENT.
    """
    import sqlite3 as _sq
    try:
        con = _sq.connect(_CORPUS_DB, uri=True, timeout=timeout)
        con.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        con.execute("SELECT 1 FROM chunk_text LIMIT 1").fetchone()
        return con
    except _sq.OperationalError as e:
        raise CorpusLocked(f"corpus read failed: {e}") from e



def _intake_newer_than_facts(slug: str) -> bool:
    d = CASES / slug
    intake = d / "intake"
    if not intake.is_dir():
        return False
    cf = d / "CASE_FACTS.md"
    cf_m = cf.stat().st_mtime if cf.exists() else 0.0
    for p in intake.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime > cf_m:
                return True
        except OSError:
            continue
    return False


def _cases_needing_refresh() -> list[str]:
    out: list[str] = []
    if not CASES.is_dir():
        return out
    for d in sorted(CASES.iterdir()):
        if d.is_dir() and (d / "CASE_FACTS.md").exists() and _intake_newer_than_facts(d.name):
            out.append(d.name)
    return out



# ── Posture detection: AFFIRMATIVE (plaintiff-initiating new complaint) vs DEFENSIVE (rebut an
#    opposing motion / appeal an adverse order). Deterministic from the record. Uses the same
#    adverse-artifact signal the audit engine relies on — no new logic, just a shape switch. ──
def _posture(slug: str) -> str:
    """AFFIRMATIVE = we filed and are prosecuting; DEFENSIVE = we are rebutting an
    opposing motion / appealing an adverse order.

    A CASE_TIMELINE.json alone is NOT a defensive signal — every case gets a timeline
    the moment any docket event lands, including cases the user filed. Keying posture
    on file-existence routed a plaintiff-side case (Progressive 260100001) into the
    defensive task list, whose `latest_order` prompt then read the DEFENDANT's proposed
    order as an entered disposition and wrote "DISMISSED with prejudice" into the record.
    Posture is decided by WHO INITIATED, read from the registry, with adverse-disposition
    artifacts as the only override.
    """
    base = CASES / slug
    import json as _j

    # 1) registry: are we the plaintiff/petitioner who opened this matter?
    try:
        for _rf in ("cases_index/case_map.json", "cases_index/case_registry.json"):
            _p = ROOT / _rf
            if not _p.exists():
                continue
            _d = _j.loads(_p.read_text(encoding="utf-8"))
            for _k, _v in (_d.items() if isinstance(_d, dict) else []):
                if not isinstance(_v, dict):
                    continue
                if _k != slug and _v.get("slug") != slug and str(
                        _v.get("folder_path", "")).replace("\\", "/").rstrip("/").split("/")[-1] != slug:
                    continue
                _role = str(_v.get("our_role") or _v.get("posture") or "").lower()
                if _role:
                    return "defensive" if _role.startswith(("def", "respond", "appell")) else "affirmative"
                # a case we commenced carries our own claims taxonomy
                if _v.get("claims") or _v.get("case_type") in ("bad_faith",):
                    _init = "affirmative"
                    break
    except Exception:
        pass

    # 2) override: a real ADVERSE disposition against us (dismissal/judgment) flips it
    try:
        adir = ROOT / "output" / "audit"
        tail = slug.split("-")[-1]
        for a in adir.glob("data_*.json"):
            if tail and tail in a.name:
                d = _j.loads(a.read_text(encoding="utf-8"))
                if d.get("dismissal"):
                    return "defensive"
    except Exception:
        pass

    # 3) docket_events.md is written by the removal/defensive intake lane only
    if (base / "docket_events.md").exists():
        return "defensive"
    return "affirmative"

_AFFIRMATIVE_HEADERS = {
    "aff_initial": "## INITIAL CLAIM",
    "aff_parties": "## PARTIES & CITIZENSHIP",
    "aff_claims":  "## CAUSES OF ACTION AND ELEMENTS",
    "aff_facts":   "## OPERATIVE FACTS",
    "aff_damages": "## DAMAGES & RELIEF",
    "aff_forum":   "## FORUM & KEEP-IN-STATE",
}
_SECTION_HEADERS.update(_AFFIRMATIVE_HEADERS)

_EVIDENCE_CONTRACT = (
    " EVIDENCE INTEGRATION CONTRACT: any '=== EVIDENCE' block flagged NEWER THAN CASE_FACTS "
    "carries facts that OVERRIDE and MUST be woven into your section verbatim (amount, number, "
    "date + source file). Echoing the prior section while ignoring flagged evidence is an "
    "INVALID output.")


def _affirmative_section_tasks(slug: str) -> list:
    base = f"obsidian_sync/cases/{slug}"
    return [
        ("aff_initial",
         f"Read {base}/intake/_ocr, the ORIGINAL complaint/petition (the FIRST filing that opened the matter, "
         f"including any state-court filing later removed), and every analysis sidecar in {base}/ (scorecard*.md, "
         f"audit*.md). Write the INITIAL CLAIM record: (1) the ORIGIN CLAIM as first pleaded (e.g. Quiet Title, "
         f"breach of contract, FCRA) with docket + date + court; (2) the CLAIM THEORY in one paragraph — what "
         f"right, what instrument/property, what invasion; (3) the INSTRUMENT/PROPERTY at issue (policy number, "
         f"note+mortgage, account, judgment) and each party's claimed relationship to it (holder, servicer, "
         f"assignee, real party in interest — name every gap: broken chain of title, missing assignment, no "
         f"standing); (4) the CURRENT POSTURE (removed/dismissed/appeal/reconsideration) and how it transforms "
         f"but never erases the origin claim. Every fact from the record with its source document; NO invention. "
         f"UPDATE ONLY a '## INITIAL CLAIM' section of {base}/CASE_FACTS.md placed FIRST after the title (create "
         f"if absent; preserve every other section). This section is the case's identity — the claim-core terms "
         f"here MUST appear or the downstream organs are blind (exposure map, litmus, drafter all key on it)."
         + _EVIDENCE_CONTRACT),
        ("aff_parties",
         f"Read {base}/intake/_ocr and the case registry. Identify the PLAINTIFF and EACH DEFENDANT with its "
         f"citizenship for diversity: a corporation = state of incorporation + principal place of business; an "
         f"LLC/partnership = the citizenship of EVERY member (Carden v. Arkoma Associates, 494 U.S. 185, and Zambelli Fireworks v. Wood, 592 F.3d 412). Flag any LLC defendant whose "
         f"member chain must be traced. UPDATE ONLY a '## PARTIES & CITIZENSHIP' section of {base}/CASE_FACTS.md "
         f"(create if absent; preserve every other section). Invent no parties." + _EVIDENCE_CONTRACT),
        ("aff_claims",
         f"SCOPE FIRST: plead counts ONLY against the defendant(s) of THIS matter — read any intake MATTER_SCOPE_* "
         f"record first (it is the user's stated scope and CONTROLS), then the PARTIES section / case registry. A wrong "
         f"committed by a non-party, or a claim the scope record assigns to another docket, is a background FACT "
         f"supporting this matter's counts, never its own count here. If the defendant is a government agency, its "
         f"duties come from the statute that binds the agency (not state consumer law). "
         f"From the intake facts ONLY, list each CAUSE OF ACTION the plaintiff can plead, with (a) its ELEMENTS, "
         f"(b) the governing PA statute or common-law rule VERBATIM from the corpus (e.g. UTPCPL 73 P.S. 201-1 / "
         f"201-9.2; money had and received; unjust enrichment; negligent misrepresentation; insurable interest), "
         f"and (c) for EACH element a PLEADING-READY paragraph: a distinct declarative averment in complaint "
         f"voice ('Defendant did X on DATE (Exhibit En)'), each element supported by a DIFFERENT record fact "
         f"wherever the record allows — never repeat one fact-list across elements, never quote the plaintiff's "
         f"own dispute letters as proof, never write 'the intake record' or 'the corpus' (write 'Defendant's "
         f"records' / 'the attached exhibits'). Distinguish elements sharply: for reliance say what plaintiff "
         f"did BECAUSE of defendant's statement; for loss name the dollar amounts; for a specific fund name it. "
         f"For BAD FAITH knowledge elements: plead the defendant's INTERNAL knowledge (its own receipts, "
         f"confirmations, system records) against its outward denials — NEVER rest a bad-faith element on a "
         f"regulator's conclusion (a regulator's closure is the defendant's shield, not the plaintiff's sword; "
         f"the contradiction between what defendant knew and what it told the regulator IS the bad faith). "
         f"Cite ONLY corpus-verified authority — invent NO citation. UPDATE ONLY a "
         f"'## CAUSES OF ACTION AND ELEMENTS' section (create if absent; "
         f"preserve every other section)." + _EVIDENCE_CONTRACT + _m2_feedback(slug, "aff_claims")),
        ("aff_facts",
         f"Extract the OPERATIVE FACTS from {base}/intake/_ocr verbatim (dates, amounts, parties, documents), "
         f"each with its source document. Do NOT invent facts, dates, or amounts. UPDATE ONLY a "
         f"'## OPERATIVE FACTS' section (create if absent; preserve every other section)." + _EVIDENCE_CONTRACT),
        ("aff_damages",
         f"State the DAMAGES from the record: actual (each amount + source), statutory/treble where an Act "
         f"provides it (cite the section), punitive where willful/bad-faith is supported, and the good-faith "
         f"demand (St. Paul Mercury Indemnity Co. v. Red Cab Co., 303 U.S. 283 (1938), controls absent legal certainty). UPDATE ONLY a "
         f"'## DAMAGES & RELIEF' section (create if absent; preserve every other section)." + _EVIDENCE_CONTRACT
         + _m2_feedback(slug, "aff_damages")),
        ("aff_forum",
         f"Determine FORUM + keep-in-state: (1) is every count state-law (no federal question on the face — "
         f"well-pleaded complaint rule)? (2) is complete diversity defeated by a non-diverse defendant (trace "
         f"any LLC member chain)? (3) forum-defendant rule. Conclude which court and why removal fails "
         f"(Shamrock Oil & Gas Corp. v. Sheets, 313 U.S. 100 (1941); Hain Celestial Group, Inc. v. Palmquist (2026); Royal Canin U.S.A., Inc. v. Wullschleger (2025)). UPDATE ONLY a '## FORUM & KEEP-IN-STATE' section "
         f"(create if absent; preserve every other section)." + _EVIDENCE_CONTRACT),
    ]


_M2_ROUTE = {"remedy": "aff_damages"}      # every other m2 axis feeds the count itself (aff_claims)


def _m2_feedback(slug: str, section: str) -> str:
    """m2's GAP findings for this case, verbatim, as constraints on the m0 section that writes the counts.
    m2 found N.J.S.A. 59:2-4 (public-entity immunity for failure to enforce) unanswered in nj_doi Count I as a
    capacity GAP; nothing carried it back and the count was rewritten by hand (F3 2026-10-21). Latest m2 run only."""
    import json as _j
    runs = sorted((CASES / slug / "drafts" / "_m2").glob("CLAIMS_*.m2"))
    if not runs:
        return ""
    run, rows = runs[-1], {}
    for f in sorted(run.glob("*.json")):
        axis = f.stem
        if axis in ("MATRIX", "cells") or _M2_ROUTE.get(axis, "aff_claims") != section:
            continue
        try:
            data = _j.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in (data if isinstance(data, list) else []):
            if not isinstance(c, dict) or str(c.get("status", "")).upper() != "GAP":
                continue
            gap = " ".join(str(c.get("gap") or c.get("finding") or "").split())
            if gap:
                rows.setdefault(str(c.get("claim", "?")), []).append(f"[{axis}] {gap[:420]}")
    if not rows:
        return ""
    lines = []
    for k in sorted(rows, key=lambda x: (len(x), x)):
        lines.append(f"Count {k}:")
        lines += [f"  - {r}" for r in rows[k][:8]]
    return ("\n\n=== m2 ANALYSIS FINDINGS ON THE PRIOR DRAFT (" + run.name + ") -- ANSWER EVERY ONE ===\n"
            "The m2 analysis bots read the prior draft of these counts against the corpus and found the gaps below "
            "(their words). For EACH finding decide what it is and act:\n"
            "(a) a DEFENSE, IMMUNITY, NOTICE/PRESENTMENT, CAPACITY or ELEMENT the count does not answer -> the count "
            "MUST answer it: plead the record facts that take the claim outside it, quoting the enacted text it names "
            "if that text is in the record above, or (if the record cannot answer it) say so plainly and narrow or "
            "drop the count -- never leave it silent;\n"
            "(b) an AUTHORITY the corpus does not hold (no case defining a term, etc.) -> do NOT cite case law for it; "
            "rest on the enacted text and write 'NOT IN CORPUS: <what>' once for that point;\n"
            "(c) a finding about a different jurisdiction's law or a different posture -> ignore it.\n"
            + "\n".join(lines) + "\n")


_ENACTED_SEC = __import__("re").compile(
    r"(?:§+\s*)?\b\d+[A-Z]?:\d+[A-Z]?-\d+(?:\.\d+)?\b"   # N.J.S.A. (§) 59:2-4, 12A:1-308
    r"|§+\s*\d+[A-Za-z]?(?:[.\-]\d+)*(?![\d:])")          # 42 U.S.C. § 1983, 13 Pa.C.S. § 1308


def _m2_unanswered(slug: str, section: str, block: str) -> list:
    """m2 GAP findings (this section's axes) naming an enacted section the block never mentions."""
    import json as _j, re as _r
    runs = sorted((CASES / slug / "drafts" / "_m2").glob("CLAIMS_*.m2"))
    if not runs:
        return []
    flat = _r.sub(r"[\s§]+", " ", block or "")
    out = []
    for f in sorted(runs[-1].glob("*.json")):
        if f.stem in ("MATRIX", "cells") or _M2_ROUTE.get(f.stem, "aff_claims") != section:
            continue
        try:
            data = _j.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in (data if isinstance(data, list) else []):
            if not isinstance(c, dict) or str(c.get("status", "")).upper() != "GAP":
                continue
            gap = " ".join(str(c.get("gap") or c.get("finding") or "").split())
            secs = {_r.sub(r"[\s§]+", " ", m).strip() for m in _ENACTED_SEC.findall(gap)}
            miss = sorted(x for x in secs if x and x not in flat)
            if miss:
                out.append((str(c.get("claim", "?")), f.stem, miss, gap[:420]))
    return out


def _premise_block() -> str:
    """The premise audit (pipeline/premise_audit.py) is the SINGLE definition of the
    question-discipline; m0 and the bot curriculum both render it from there so the
    method can never drift between them."""
    try:
        from pipeline.premise_audit import questions_block
        return questions_block()
    except Exception:
        return ""


def _pending_adverse_filing(slug: str) -> dict | None:
    """Is there an UNANSWERED adverse party filing in this case's intake?

    Posture (who filed the case) and adverse-filing-present are INDEPENDENT facts. A
    plaintiff prosecuting his own case still gets attacked, and when he does the case needs
    BOTH lanes: its own claims AND the rebuttal analysis of the attack. Keying the rebuttal
    task to posture alone routed an affirmative case away from the only organ that analyzes
    an opposing filing — m0 re-described the plaintiff's own claims and never read the
    preliminary objections sitting in its intake (caught 2026-09-23: zero findings on the
    enacted text, the binding-weight ledger, or the stretched holdings).

    Deterministic: uses is_entered_order() to tell a PARTY filing from a court order, and
    ignores anything older than the last CASE_FACTS write (already-analyzed).
    """
    base = CASES / slug
    idir = base / "intake"
    if not idir.is_dir():
        return None
    cf = base / "CASE_FACTS.md"
    cf_mtime = cf.stat().st_mtime if cf.exists() else 0
    newest = None
    for fp in list(idir.glob("*.txt")) + list(idir.glob("*.doctext.txt")):
        try:
            st = fp.stat()
        except Exception:
            continue
        if st.st_size < 800:
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")[:20000]
        except Exception:
            continue
        cls = is_entered_order(txt)
        # a party filing (or a party filing carrying a proposed order) is an ADVERSE paper;
        # an entered order is the latest_order lane's job, not the rebuttal lane's.
        if cls.get("kind") not in ("party_filing", "proposed_order_in_party_filing"):
            continue
        # our OWN filings are not adverse — skip anything signed by the pro-se party
        if re.search(r"/s/\s*Filer|sui juris", txt, re.I):
            continue
        if newest is None or st.st_mtime > newest[1]:
            newest = (fp, st.st_mtime, cls)
    if not newest:
        return None
    fp, mt, cls = newest
    return {"path": str(fp), "name": fp.name, "kind": cls.get("kind"),
            "analyzed": mt <= cf_mtime}


def _section_tasks(slug: str) -> list[tuple[str, str]]:
    """Break the case-facts UNDERSTANDING into context-sized pieces. Each piece reads only its slice of
    the record, is grounded by the deterministic docket (tie_docket pinned the real ECF#s in
    CASE_TIMELINE.json), and writes its part to CASE_FACTS.md. The pieces connect THROUGH OBSIDIAN -- the
    shared CASE_FACTS file + the connected_notes graph -- so the full context reassembles without one
    giant prompt. Never collapse this to deterministic-only; that strips the substance. Order: the later
    sections build on the earlier via -c continuity AND the file they just wrote."""
    base = f"obsidian_sync/cases/{slug}"
    if _posture(slug) == "affirmative":
        tasks = _affirmative_section_tasks(slug)
        # An affirmative case UNDER ATTACK needs both lanes: its own claims AND the
        # rebuttal analysis of the adverse filing. Append the defensive motion+rebuttal
        # tasks so the premise audit and rules 10-15 reach the opposing paper.
        adv = _pending_adverse_filing(slug)
        if adv:
            _def = dict(_defensive_section_tasks(slug))
            for k in ("motion", "rebuttal"):
                if k in _def:
                    tasks.append((k, _def[k] +
                                  f" THE ADVERSE FILING TO ANALYZE IS {adv['name']} in "
                                  f"{base}/intake (classified {adv['kind']}). Read THAT document."))
        return tasks
    return _defensive_section_tasks(slug)


def _defensive_section_tasks(slug: str) -> list[tuple[str, str]]:
    base = f"obsidian_sync/cases/{slug}"
    # INITIAL CLAIM leads BOTH postures (class rule, 2026-08-31: 'it looks at
    # the case but doesnt look at the INITIAL CLAIM AT STATE LEVEL' — the
    # defensive lane especially, where removal/dismissal posture buries the
    # origin claim and downstream organs map only the periphery).
    _ic = _affirmative_section_tasks(slug)[0]
    return [
        _ic,
        ("latest_order",
         f"FIRST — ENTERED-ORDER GATE (mandatory, do this before anything else): a document is an "
         f"ENTERED order ONLY if it bears the court's own execution — an attestation/filing stamp from "
         f"the clerk or prothonotary on the ORDER ITSELF plus a signed judicial signature line (a DATED "
         f"'BY THE COURT: /s/ <judge>' or equivalent). A [PROPOSED] ORDER attached to a party's motion, "
         f"preliminary objections, or brief is NOT an order: it is the MOVANT'S REQUESTED relief. Its "
         f"tells are a blank date ('NOW, this ____ day of ______'), an unsigned signature line ('______ J.'), "
         f"and a party's e-filing stamp/Control No. rather than an order docket entry. The filing stamp on a "
         f"motion packet attests that the PARTY filed it, never that a judge granted it. If the newest "
         f"document in {base}/intake is a proposed order inside a party filing, you MUST NOT report its "
         f"requested relief as a disposition: instead write the section as 'NO NEW ENTERED ORDER — the "
         f"newest filing is <party>'s <document> (Control No.), whose proposed order requests <relief>; "
         f"the court has NOT ruled', name the last actually-entered order (or 'none'), and STOP. Never "
         f"attribute authorities to 'the court' that appear only in a party's brief — those are the "
         f"MOVANT'S citations. Invent no case names: every authority you name must appear verbatim in the "
         f"document you are reading.\n\n"
         f"IF AND ONLY IF an entered order exists: read the NEWEST court order/opinion in {base}/intake "
         f"(the HIGHEST Dkt/ECF number — e.g. a just-entered "
         f"memorandum, opinion, or final judgment) and the pinned {base}/CASE_TIMELINE.json. Determine the CURRENT "
         f"operative disposition and write it as the section the drafter anchors to. State: (a) that THIS newest "
         f"order is the OPERATIVE disposition and ANY earlier 'latest order' is SUPERSEDED / prior-phase (a final "
         f"judgment supersedes all prior interlocutory orders); (b) its ECF# + date + exact holding-by-holding "
         f"disposition (what it granted/denied/dismissed/closed); (c) the specific AUTHORITIES the order relied on, "
         f"and — cross-checking the scorecard in output/audit/data_*.json — which are NON-BINDING (out-of-circuit / "
         f"district authority presented as controlling; NAME them) versus the controlling Supreme Court / this-circuit "
         f"authority the order should have applied; (d) any record matter the order overlooked (e.g. ECF 112). UPDATE "
         f"ONLY the '## LATEST COURT ORDER' section of {base}/CASE_FACTS.md (create if absent; preserve every other "
         f"section). A rebuttal/vacatur/brief attacking the disposition MUST anchor to THIS order's own reasoning and "
         f"the authorities IT cited — never a prior order's. Ground via search_chunks; cite the record + rule; no "
         f"label; invent nothing."),
        ("parties",
         f"Case {base}: read the NEWEST filing in {base}/intake (prefer dkt_*/.ocr.txt). From its "
         f"signature/certificate block, identify the OPPOSING COUNSEL (name + firm) and the JUDGE. UPDATE "
         f"ONLY the DEFENDANT POSTURES section of {base}/CASE_FACTS.md with that counsel; preserve every "
         f"other section. Cite the filing line; do not invent."),
        ("motion",
                 f"Same filing: the ECF number is ALREADY pinned in {base}/CASE_TIMELINE.json -- read it there, do "
                 f"not guess. State the ECF#, filing date, and the EXACT relief sought. "
                 f"PENDING means PENDING: a motion/objection entry records what the MOVANT ASKED FOR and "
                 f"nothing more. Do NOT write a disposition, holding, ruling, or 'Court's Disposition' field "
                 f"unless a SEPARATE document bearing judicial execution (a dated, signed 'BY THE COURT' line, "
                 f"'SO ORDERED', or a judge's /s/) says so. A [PROPOSED] ORDER attached to the movant's own "
                 f"filing is the RELIEF REQUESTED — quoting its 'GRANTED ... DISMISSED with prejudice' language "
                 f"as the court's action is a FABRICATED RULING and the single worst error in this pipeline "
                 f"(it happened twice on this codebase). A party's e-filing stamp attests that the PARTY filed "
                 f"the paper, never that a judge acted. If no separately-executed order exists, state "
                 f"'Status: PENDING — no ruling entered' and stop. "
                 f"UPDATE ONLY PENDING MOTIONS "
                 f"and DOCKET HIGHLIGHTS of {base}/CASE_FACTS.md with that one entry; preserve every other section. Cite."),
        ("rebuttal",
         f"For that motion, run a COMPREHENSIVE DEFECT & LEVER analysis — do NOT stop at one argument; catch "
         f"EVERYTHING the record + corpus support so nothing has to be pointed out by hand. Test the movant AND "
         f"its filing against the FULL universe and record EACH that applies, grounded via search_chunks (corpus "
         f"decides) + the record ({base}/intake, {base}/CASE_TIMELINE.json, {base}/CASE_FACTS.md) + the Obsidian "
         f"graph ([[filings-rules-decision-map]], [[antifrivolous]], [[special-appearance-abolished]], "
         f"[[real-party-enforce]], [[person-slide]]): (1) STANDING — Article III / statutory / prudential / real "
         f"party in interest (Rule 17); (2) CAPACITY — Rule 17(b) / charter / who may sue or be sued; (3) "
         f"JURISDICTION — subject-matter & personal (12(b)(1)/(2)); void order under Rule 60(b)(4); (4) DEFAULT — "
         f"is the MOVANT itself in default / did it answer untimely (Rule 55)? a party in default cannot obtain "
         f"affirmative relief; (5) SERVICE / PROCESS — 12(b)(4)/(5); (6) ADMISSIONS BY CONDUCT in the record "
         f"(e.g. a tradeline removed, a payment / 1099-C / check tendered, the movant's OWN letters) — matters "
         f"OUTSIDE the pleadings that convert a Rule 12(c) / 12(b)(6) motion to Rule 56 (Rule 12(d)) FOR the "
         f"plaintiff; (7) the frivolous / pseudolaw-label rebuttal (Neitzke arguable-basis); (8) capacity / "
         f"definition SLIDES; (9) RECIPROCITY / MUTUALITY — if the order or filing imposes a WAIVER, DEADLINE, "
         f"or DUTY on the plaintiff ('comply or waive', 'you failed to X'), test whether the SAME standard "
         f"defeats the imposing party: a court-imposed waiver is a RULE not a statute, and a standard binding "
         f"one side only is void for want of MUTUALITY (no unilateral agreements; 'equality is equity'; 'he who "
         f"seeks equity must do equity'; no approbate-and-reprobate) [ground via search_chunks]; an opposing "
         f"garnishee/movant that missed its OWN deadline (Pa.R.C.P. 3144(b) 20-day answer to garnishee interrogatories; failure to answer = default judgment under Pa.R.C.P. 3146(a)) or filed UNVERIFIED "
         f"(1024/1029(b) deemed admission) has itself WAIVED/defaulted and cannot obtain affirmative relief; and "
         f"the court's own MANDATORY duties (Pa.R.A.P. 1925(a), oath, canons) + the limits of judicial immunity "
         f"(judicial acts WITH jurisdiction only, Stump v. Sparkman; prospective relief not barred, Pulliam v. "
         f"Allen) apply reciprocally. For EACH that applies, state it with its RULE + a RECORD cite. "
         + " " + _premise_block() + " " +
         _relabel_block(slug) + _waiver_block(slug) + f"(10) ANSWER FROM OUR PLEADING, NOT THEIRS — before accepting ANY characterization of a count, "
         f"read what {base}/CASE_FACTS.md 'CAUSES OF ACTION AND ELEMENTS' and the filed complaint ACTUALLY "
         f"plead. If the movant attacks a theory we did not plead (e.g. objecting to a TORT of bad faith "
         f"when the count pleads the CONTRACTUAL implied covenant; objecting to a claim 'in the insurance "
         f"context' when the claim is a general common-law doctrine), the objection is NON-RESPONSIVE and "
         f"MOOT for want of a pleaded target — say so and demand they identify BY COUNT AND PARAGRAPH where "
         f"the theory they attack appears. NEVER propose repleading our count into the shape their brief "
         f"assumes, and NEVER narrow a general doctrine to the movant's industry: unjust enrichment, "
         f"conversion, and restitution are general common-law doctrines that apply to ANY defendant "
         f"regardless of industry — a movant's framing is argument, never a fact about the law. "
         f"(11) BINDING-WEIGHT LEDGER — classify EVERY authority the movant cites by issuing court and say "
         f"whether it binds THIS forum. In a Pennsylvania Court of Common Pleas only PA Supreme Court and PA "
         f"Superior Court decisions bind on questions of PA law; federal district (E.D./W.D. Pa.), Third "
         f"Circuit, and out-of-state decisions are PERSUASIVE ONLY (a federal court sitting in diversity "
         f"predicts state law, it does not make it); U.S. Supreme Court binds only on FEDERAL questions. "
         f"Flag any categorical proposition of state law propped up mainly by non-binding authority, and "
         f"NAME the controlling state high-court construction the brief omitted. "
         f"(12) STRETCHED-HOLDING CHECK — never characterize a cited case from the movant's own summary of "
         f"it. Read the opinion text (search_chunks; if absent, mark NOT-VERIFIED and say it must be pulled "
         f"via pipeline.case_fetch before use). State what the case actually held, then whether the movant's "
         f"proposition exceeds it. "
         f"(13) WAIVER SCOPE — state waiver EXACTLY as the rule states it, never broader. Under Pa.R.C.P. "
         f"1028(b)+1032(a) a party that omitted a ground from its single set of preliminary objections is "
         f"foreclosed from further PRELIMINARY OBJECTIONS, but 1032(a) expressly EXCEPTS failure to state a "
         f"claim and failure to join an INDISPENSABLE party (1032(b)), and defenses may still be raised by "
         f"answer or new matter. Overstating waiver is itself a defect the opponent will flip. "
         f"(14) REMEDY-STRIPPING — cross-reference WHICH counts were attacked against each count's REMEDY "
         f"schedule (treble/fees, punitive, restitution-disgorgement vs contract expectancy). Objections "
         f"clustering on the high-multiplier counts while sparing the narrowest-recovery counts is a "
         f"remedy-stripping posture and an implicit concession that the spared counts state claims "
         f"(pipeline.remedy_strip_detector flags this deterministically — fold its finding in). "
         f"(15) TREATISE CONFRONTATION (run on EVERY case, subject-derived — never a fixed citation "
         f"list). When the movant asserts a CATEGORICAL rule ('X never applies to Y', 'claims of this "
         f"type are always barred'), derive the SUBJECT of that rule from the filing itself (the claim "
         f"type, the statute, the doctrine, the industry) and search_chunks the corpus's treatise shelf "
         f"for that subject's settled statement — Am. Jur. 2d (source keys begin `amjur`; e.g. an auto "
         f"insurance dispute lands in the automobile-insurance volume, a trusts question in the trusts "
         f"volume), plus Proof of Facts / Legal Forms volumes when a proof roadmap exists for the fact "
         f"pattern, plus C.J.S. and Restatements where present. Let the SUBJECT pick the volume; never "
         f"hardcode a volume, and never assume a subject is absent because one phrasing missed. If the "
         f"treatise states the rule WITHOUT the movant's limitation, record the confrontation: quote the "
         f"treatise verbatim with its [corpus:<source>] tag, state that the movant's brief neither cites "
         f"nor distinguishes it, and frame the demand — Defendant must either accept the settled "
         f"statement (which defeats its own categorical rule) or state affirmatively that the settled "
         f"rule is wrong and on what authority. A party asking a court to depart from the settled "
         f"national statement of a doctrine bears the burden of saying so out loud. Treatises are "
         f"persuasive, not binding — say so plainly and pair each with the BINDING authority it "
         f"summarizes; the force is that silence against a stated authority is itself record. If the "
         f"corpus has no treatise on the subject, write NOT-IN-CORPUS and name the volume to ingest — "
         f"never fabricate a treatise section number. "
         f"(17) WRITE THE SPINE AUTHORITY INTO THE ANALYSIS. The CONTROLLING AUTHORITY block "
         f"in your context is corpus-verbatim and already verified — it is not background "
         f"reading, it is the authority this response must cite. Every controlling case and "
         f"every enacted subsection in that block that bears on an attacked claim MUST appear "
         f"BY NAME AND CITE in your analysis, with the operative words quoted. An authority "
         f"that reaches your context but not your output is an authority the drafter cannot "
         f"use and the court will never see. Where the movant omitted a controlling authority "
         f"that the block supplies, say so explicitly: name it, quote its holding, and state "
         f"that the brief neither cites nor distinguishes it. "
         f"(18) ANSWER THE OBJECTIONS WITH THE RECORD, NOT ONLY WITH LAW. For each objection, "
         f"state the RECORD FACT that defeats it — the dated document, the amount, the "
         f"quotation from the movant's OWN writing — not merely the legal rule. A premise "
         f"like 'the relationship is wholly contractual' is defeated by a fact (what happened "
         f"and when), and an argument that recites doctrine without the fact that triggers it "
         f"is half an answer. Pull these from the case's evidence/exhibit register and quote "
         f"them; where two of the movant's own statements contradict each other, put them "
         f"side by side with both dates — that pair is the strongest material in the record. "
         f"(16) PINPOINT GRANULARITY — cite the SUBSECTION that does the work, never the act "
         f"or chapter generally. '73 P.S. § 201-1 et seq.' or '42 Pa.C.S. ch. 83' tells a court "
         f"nothing; the operative unit is the defined term, the exclusions clause, the remedy "
         f"section, the specific rule subdivision. Quote the operative words and give their "
         f"exact locator. Same for cases: a pin cite to the page carrying the holding, and the "
         f"holding's own words — never a bare case name for a proposition. Where a filing "
         f"attacks a claim, name the subsection that answers it and the subsection they omitted. "
         f"Write the grounded, "
         f"cited analysis as an 'ADVERSE-FILING DEFECTS & LEVERS' block under the PENDING MOTIONS entry in "
         f"{base}/CASE_FACTS.md (preserve every other section). Then append ONE line to output/learnings.jsonl "
         f"as JSON {{\"text\": <the strongest ground in one sentence>, \"tags\": [\"{slug}\",\"defects\"], "
         f"\"source\": \"{slug}\"}} so connected_notes links it into the graph. Cite the record + the rule; no "
         f"label; invent nothing — only what the record + corpus actually support."),
        ("disposition",
         f"WHOLE-RECORD cross-order pass (NOT a single filing): read {base}/CASE_TIMELINE.json (every pinned "
         f"docket entry) and the DOCKET HIGHLIGHTS + PENDING MOTIONS of {base}/CASE_FACTS.md, AND the NEWEST "
         f"order/opinion text in {base}/intake (e.g. a just-entered memorandum or final judgment). Identify EVERY "
         f"adverse / appealable / dispositive order in the whole record -- not just the newest. UPDATE ONLY an "
         f"'APPEAL & DISPOSITION SCOPE' section of {base}/CASE_FACTS.md (create it if absent; preserve every "
         f"other section) with: (a) the SINGLE OPERATIVE (current, controlling) order FIRST -- the newest "
         f"dispositive order that fixes the present posture (a final judgment / case-closing order supersedes all "
         f"prior interlocutory orders; state its ECF#, date, exact disposition, the specific authorities it relied "
         f"on, and whether those authorities are BINDING on this court or merely out-of-circuit/district persuasive "
         f"per the scorecard) -- and label every EARLIER adverse order as PRIOR-PHASE / SUPERSEDED (still appealable "
         f"and listed, but NOT the operative disposition); (b) EVERY appealable adverse order (control/ECF# + date + "
         f"one-line disposition); (c) the SINGLE controlling EARLIEST appeal deadline and which order drives it; "
         f"(d) the appealability hook per order, grounded via search_chunks -- corpus decides (e.g. Pa.R.A.P. "
         f"311/903/341 or 28 U.S.C. 1291 / 1292 / Rule 54(b)); (e) the SHARED merits/legal rebuttal common across "
         f"the orders. State plainly that any Notice of Appeal or omnibus response MUST list ALL of these orders, "
         f"not only the latest, BUT that a rebuttal/vacatur of a specific order must be anchored to THAT order's "
         f"own reasoning and the authorities IT cited -- not a prior order's. Cite the record + the rule; no label; "
         f"do not invent."),
        ("order_errors",
         f"COURT-ERROR pass over EVERY adverse ORDER (the COURT's own orders, not the movant's motions). Read "
         f"{base}/CASE_TIMELINE.json, the DOCKET HIGHLIGHTS + 'APPEAL & DISPOSITION SCOPE' of {base}/CASE_FACTS.md, "
         f"and any order text in {base}/intake. Many adverse orders are BARE ONE-LINERS ('DENIED IN ITS ENTIRETY') "
         f"with no findings -- a conclusory order exposes no reasoning, so YOU supply the record and the rule it "
         f"skipped. For EACH adverse order, grounded via search_chunks (corpus decides) + the record, record: "
         f"(a) the exact disposition and whether it is an unexplained one-liner with no findings of fact / "
         f"conclusions of law (the court's own duty to state reasons -- Pa.R.A.P. 1925(a) forces it on appeal); "
         f"(b) the SPECIFIC RULE or STATUTE the court had to apply and IGNORED to reach that ruling -- the ignored "
         f"rule is what makes the order erroneous (e.g. Pa.R.C.P. 3146(a) garnishee default for failing the Pa.R.C.P. 3144(b) 20-day answer, 3111(b) writ attaches "
         f"by operation of rule so no separate allegation of possession is needed, 1024 verification, 1531 "
         f"injunction prerequisites, Pa.R.E. 201 mandatory judicial notice); (c) the SUBSTANTIVE obligation behind "
         f"any garnishee/party the order ignored -- e.g. an insurer's Coverage E liability for the insured's "
         f"adjudicated judgment, or a custodian's holdings -- which makes the party a proper garnishee/defendant so "
         f"its failure to answer = default with legal consequence, and note where a prior 12(b)(6) tort dismissal "
         f"does NOT preclude that distinct garnishable obligation; (d) any RECORD FACT the order contradicts or the "
         f"court was required to judicially notice; (e) any SUA SPONTE relief granted without notice/hearing (due "
         f"process / right to be heard -- Pa. C.J.C. Rule 2.6) and any recusal / financial-interest / ex-parte / "
         f"supervising-judge issue (Pa. C.J.C. Rule 2.9 ex parte, 2.11 disqualification) -- the judicial-duty layer "
         f"for the accountability track. State plainly that any Concise Statement (Pa.R.A.P. 1925(b)) or omnibus "
         f"response must be COMPREHENSIVE (every error -- WAIVE NOTHING; omitted issues are waived under "
         f"1925(b)(4)(vii)) yet CONCISE (1925(b)(4)(iv): the NUMBER of errors alone is not waiver where each is "
         f"concise and non-redundant) and must RESERVE ALL issues. Write to an 'ADVERSE-ORDER ERRORS (court's "
         f"ignored rules)' block in {base}/CASE_FACTS.md (create if absent; preserve every other section). Cite the "
         f"record + the rule; no label; invent nothing -- only what the record + corpus support."),
    ]


def _connect_notes() -> None:
    """Tie the pieces into the Obsidian graph (deterministic, no LLM): ingest learnings.jsonl -> atomic
    [[linked]] notes + recompute links + MOC, so no piece sits alone."""
    try:
        subprocess.run([PYBIN, "-m", "pipeline.connected_notes"], cwd=str(ROOT),
                       timeout=CONNECT_TIMEOUT,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
    except Exception as e:
        print(f"  connected_notes relink skipped: {e}")


def _extract_block(stdout: str, header: str) -> str:
    """Pull the finished markdown block out of the agent's stdout: strip <think> tags, then take from the
    LAST occurrence of the canonical header line to EOF (the agent, per the output contract, emits the block
    last). Returns '' if the header never appears (-> the section is a real FAIL, not a false 'ok')."""
    if not stdout:
        return ""
    t = re.sub(r"<think>.*?</think>", "", stdout, flags=re.S | re.I)
    h = header.strip()
    lines = t.splitlines()
    # Anchor on the LAST line that IS the header standing alone (ignore code-fence backticks / trailing
    # punctuation) -- so an inline mention of the header inside prose ("write to `## PENDING MOTIONS`? Yes")
    # is NOT matched as the block start.
    start = None
    for j in range(len(lines) - 1, -1, -1):
        norm = lines[j].strip().strip("`").rstrip("`?:.,) ").strip()
        if norm == h:
            start = j
            break
    if start is None:
        return ""
    _cue = re.compile(r"^(wait\b|let me\b|let'?s\b|let us\b|i should\b|i need to\b|actually,? i\b|hmm\b|"
                      r"now,? (let|i)\b|okay,|ok,|to refine|to be (safe|precise)|on reflection|"
                      r"double.?check|i'?ll (now|refine|make)|so,? (to|in))", re.I)
    kept = [h]  # force a clean canonical header line
    for ln in lines[start + 1:]:
        s = ln.strip()
        if _cue.match(s):
            break
        # stop at the next DIFFERENT top-level section header (keep our own ### subheaders)
        if s.startswith("## ") and s.strip("`").rstrip("`?:.,) ").strip() != h:
            break
        kept.append(ln)
    return "\n".join(kept).strip()


_DISPOSITION_ASSERTION = re.compile(
    # ONLY a claim that a court HAS ACTED — past/perfect tense with a court subject.
    r"\b(?:the\s+)?(?:court|judge|tribunal|honorable)\b[^.\n]{0,60}"
    r"\b(?:granted|denied|dismissed|sustained|overruled|entered|ruled|held)\b"
    r"|court'?s?\s+(?:disposition|ruling|holding)"
    r"|\b(?:has|have|was|were)\s+(?:been\s+)?(?:granted|denied|dismissed|sustained|overruled)\b"
    r"|\bORDERED\s+AND\s+DECREED\b"
    # ACTOR-LESS DISPOSITION (added 2026-09-24, mail-watch 05:00). The actor-bearing
    # patterns above caught "the Court granted ..." but let the SAME false ruling through
    # in passive/tabular form, which is how a summary section actually writes it:
    #   "**Claims Dismissed:**" / "**Count II:** Unjust Enrichment (dismissed with prejudice)."
    # Those assert a completed judicial act with no court noun in the sentence, so the
    # guard scored them ALLOW and a purged false order re-appeared as a claims table.
    # Class-level: any claim/count asserted as disposed, in any phrasing, needs an
    # entered order in the record. _REQUESTED_RELIEF still exempts requested/negated text.
    r"|\bclaims?\s+dismissed\b|\bcounts?\s+dismissed\b|\bdismissed\s+claims?\b"
    r"|\b(?:counts?|claims?)\b[^.\n]{0,80}?\b(?:dismissed|disposed\s+of|stricken)\b"
    r"[^.\n]{0,30}?\bwith(?:out)?\s+prejudice\b"
    r"|\(\s*(?:dismissed|denied|granted|sustained|overruled|stricken)\b[^)\n]{0,40}\)",
    re.I)

# Language DESCRIBING requested/hypothetical relief is not a claim that a court acted.
# The first live run of this guard rejected four legitimate sections because a sentence
# merely reciting "Defendant seeks dismissal with prejudice" tripped it.
_REQUESTED_RELIEF = re.compile(
    r"\[PROPOSED\]|\bproposed order\b|\bwherefore\b|\brespectfully requests?\b"
    r"|\bmovant (?:seeks|requests|asks)\b|\bprays? (?:for|that)\b"
    r"|\bseeks? (?:dismissal|judgment|an order)\b"
    # NEGATION IS THE CORRECT FINDING, NOT A FABRICATED RULING. A sentence DENYING that
    # a court has acted ("no court has yet ruled", "this is a party's motion, not a court
    # ruling", "the objections remain pending") is exactly what m0 should write when no
    # order exists. Rejecting it destroyed whole sections.
    r"|\bno court has\b|\bnot (?:yet )?(?:been )?(?:ruled|decided|disposed)\b"
    r"|\bhas not (?:yet )?(?:ruled|granted|denied|dismissed|sustained|decided)\b"
    r"|\bremains? pending\b|\bstill pending\b|\bawait\w+ (?:a )?(?:ruling|decision)\b"
    # NEGATION GAP (fixed 2026-09-24): the old alternation exempted "no order has been
    # entered" and "no court has entered" but NOT the most natural phrasing of the same
    # correct finding -- "no COURT ORDER has been entered" -- because the noun phrase puts
    # a word between "no" and "order". The guard therefore rejected the very sentence it
    # exists to elicit. Widened to any adjectival run before the noun.
    r"|\bno\s+(?:entered\s+|signed\s+|court\s+|judicial\s+|final\s+|written\s+)*"
    r"(?:order|ruling|disposition|decision|judgment)\b"
    r"|\bnot (?:a |an )?(?:court |judicial )?(?:ruling|order|disposition|decision)\b"
    r"|\bparty['’]?s? (?:own )?(?:motion|filing|objections?|brief)\b"
    # negated attribution: "it does NOT attest that a court granted it",
    # "the stamp does not show the court ruled", "this does not mean the court dismissed"
    r"|\b(?:does|did|do|has|have|is|was|are|were)\s+not\s+[\w\s]{0,30}?"
    r"(?:that\s+)?(?:a|the|this)\s+(?:court|judge|tribunal)\b"
    # limited attribution: "attests ONLY that a party filed it", "shows only that"
    r"|\b(?:attests?|shows?|reflects?|establishes?|indicates?|means?)\s+only\s+that\b"
    r"|\bonly\s+that\s+(?:a|the)\s+part(?:y|ies)\b"
    r"|\bfiling\s+stamp\b|\btime[- ]?stamp(?:ed)?\b"
    r"|\brelief (?:being )?requested\b|\bwhat .{0,20}asks the court\b",
    re.I)


def _record_has_entered_order(slug: str) -> bool:
    """Does this case's intake actually contain a document bearing JUDICIAL EXECUTION?

    The deterministic half of the proposed-order guard. An LLM instruction not to report a
    party's requested relief as a ruling is a request; this is the check. Scans the case
    intake once and asks is_entered_order() — which keys on a signed/dated 'BY THE COURT',
    'SO ORDERED', or a judge's /s/, and rejects the proposed-order shape (blank date,
    unsigned '____ J.', movant Control No.).
    """
    idir = CASES / slug / "intake"
    if not idir.is_dir():
        return False
    # HALF-BLIND EVIDENCE FINDER (fixed 2026-09-24, mail-watch 05:00). Intake holds PDFs;
    # their extracted text lives in intake/_ocr/<name>.pdf.txt. This scan globbed only the
    # TOP level, so for any case whose orders arrive as PDFs the answer was always "no
    # entered order" -- and the disposition guard then rejected TRUE statements about real
    # rulings (proved on fed_26-cv-03268, whose dkt 6 and dkt 7 orders sit in _ocr/ and were
    # invisible here). Searching _ocr/ too makes the guard's evidence match the record.
    # Verified not to loosen the proposed-order finding: progressive's _ocr contains only the
    # movant's proposed order, which is_entered_order() still classifies as NOT entered.
    for fp in (list(idir.glob("*.txt")) + list(idir.glob("*.doctext.txt"))
               + list(idir.glob("_ocr/*.txt"))):
        try:
            if fp.stat().st_size < 400:
                continue
            if is_entered_order(fp.read_text(encoding="utf-8", errors="replace")[:20000]).get("entered"):
                return True
        except Exception:
            continue
    return False


def _reject_unsupported_disposition(slug: str, header: str, block: str) -> tuple[bool, str]:
    """SECTION-AGNOSTIC guard: no section may assert a court disposition unless an entered
    order exists in the record.

    Root-cause fix for a three-month-old latent bug (surfaced 2026-09-23). The defensive lane
    was built for cases whose newest intake document is a REAL entered order — every federal
    matter on this box was. The first time an adverse PARTY FILING carrying a [PROPOSED] ORDER
    arrived ("NOW, this ___ day of ___ ... GRANTED ... DISMISSED with prejudice"), the prompts
    that say "state the disposition" faithfully reported the movant's wish list as the court's
    act — poisoning CASE_FACTS twice, in two different sections. Patching prompts is
    whack-a-mole: any task that can meet order-shaped text has the same hole. This runs at the
    single write chokepoint instead, so it covers every section that exists or is ever added.

    Returns (allowed, reason).
    """
    m = _DISPOSITION_ASSERTION.search(block or "")
    if not m:
        return True, ""
    # evaluate the MATCHED SENTENCE, not the whole block — a section may legitimately
    # describe requested relief in one sentence while asserting nothing in another.
    _b = block or ""
    _s = max(0, _b.rfind(".", 0, m.start()) + 1)
    _e = _b.find(".", m.end())
    sentence = _b[_s:_e if _e > 0 else len(_b)]
    if _REQUESTED_RELIEF.search(sentence):
        return True, ""
    if _record_has_entered_order(slug):
        return True, ""
    return False, ("asserts a court disposition but NO entered order exists in the record "
                   "(a [PROPOSED] ORDER inside a party filing is the relief REQUESTED): "
                   + " ".join(sentence.split())[:140])


def _spine_authorities_in_context(ctx: str) -> list:
    """Cites the CONTROLLING AUTHORITY block put in front of the model this run."""
    if not ctx:
        return []
    i = ctx.find("m0_authority_spine")
    if i < 0:
        return []
    blk = ctx[i:i + 30000]
    out = []
    for m in re.finditer(
            r"\*\*[^*]{3,90},\s*((?:\d{1,3}\s+[A-Z][A-Za-z.]*\.?\s?(?:2d|3d|4th)?\s+\d{1,4})"
            r"|(?:\d{1,2}\s+(?:P\.S\.|Pa\.C\.S\.)\s*§*\s*[\d.\-()]+))", blk):
        c = " ".join(m.group(1).split())
        if c not in out:
            out.append(c)
    return out


def _cite_satisfied(want: str, block: str) -> bool:
    """Is a required authority actually present, allowing a MORE PRECISE citation?

    A literal match calls the pinpoint subsection a miss: the spine registers
    '73 P.S. §§ 201-2, 201-3, 201-9.2' while a correctly-drafted analysis writes
    '§ 201-2(3)' — the exact granularity rule 16 demands. Match on the citation's
    IDENTITY (title + section root), so a narrower cite satisfies a broader one and a
    genuinely absent authority still fails.
    """
    b = (block or "").lower()
    w = " ".join((want or "").split())
    if not w:
        return True
    if w.lower() in b:
        return True
    # statute family: title + each section root ("73 P.S. §§ 201-2, 201-3" -> 201-2, 201-3)
    ms = re.match(r"(\d{1,2})\s+(P\.S\.|Pa\.C\.S\.|U\.S\.C\.)\s*§*\s*(.+)$", w, re.I)
    if ms:
        title, code = ms.group(1), ms.group(2).lower().replace(" ", "")
        roots = [r.strip() for r in re.split(r"[,;]", ms.group(3)) if r.strip()]
        for root in roots:
            root = root.lstrip("§ ").strip()
            # the block satisfies it if it cites this title+code and that section root,
            # with or without a subsection suffix
            if re.search(rf"{title}\s*{re.escape(ms.group(2).lower())}[^\n]{{0,20}}"
                         rf"{re.escape(root)}(?:\([a-z0-9]+\))*", b, re.I):
                return True
            if re.search(rf"§+\s*{re.escape(root)}(?:\([a-z0-9]+\))*", b, re.I) and \
               re.search(rf"\b{title}\s*{re.escape(ms.group(2).lower())}", b, re.I):
                return True
        return False
    # reporter cite: volume + reporter + page must all appear
    mr = re.match(r"(\d{1,3})\s+([A-Z][A-Za-z.]*\.?\s?(?:2d|3d|4th)?)\s+(\d{1,4})$", w)
    if mr:
        v, rep, pg = mr.group(1), mr.group(2).replace(" ", "").lower(), mr.group(3)
        return bool(re.search(rf"\b{v}\s*{re.escape(rep[:1])}[A-Za-z.\s]*\s*{pg}\b", b, re.I))
    return False



# DATE_ROLE_GUARD_V1 — a date is only as good as the EVENT it is attached to.
# Records routinely carry several dates whose roles are easy to swap (policy
# purchased 03/21/2019 vs vehicle sold 03/21/2023 — same month and day, four years
# apart). An analysis that attaches the wrong event to a date states a fact the
# record contradicts, and the movant will quote it back. Class-level and agnostic:
# roles are learned FROM the case's own timeline, never hardcoded.
_DATE_TOKEN = re.compile(
    r"\b(?:(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}|\d{2}/\d{2}/\d{4})\b")
# verbs that assign a role to a date, grouped by the role they assert
_ROLE_VERBS = {
    "acquire": r"purchas\w+|acquir\w+|bought|buy\w*|obtain\w+|issued|inception|effective",
    "dispose": r"sold|sell\w*|sale of|dispos\w+|transferr\w+|trad(?:e|ed|ing)",
    "cancel":  r"cancell?\w+|terminat\w+|rescind\w+|lapsed",
    "notify":  r"notifi\w+|informed|advised|reported|wrote|letter",
    "pay":     r"paid|payment|premium drawn|withdraw\w+|charged",
}

def _norm_date(d: str) -> str:
    d = d.strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    try:
        import datetime as _dt
        return _dt.datetime.strptime(d.replace(",", ""), "%B %d %Y").strftime("%Y-%m-%d")
    except Exception:
        return d


def _date_roles_from_record(cf_text: str) -> dict:
    """Learn date->role(s) from the case's own record, with COUNTS.

    Scoped to the clause around each date, not the whole line: a paragraph that
    narrates a purchase and a later sale would otherwise teach both roles to both
    dates and the guard would never fire. The nearest role verb within a short
    window wins, and we keep counts so an outlier cannot outvote the record.
    """
    roles = {}
    sents = re.split(r"(?<=[.!?;:])\s+|\n", cf_text or "")
    for sent in sents:
        for m in _DATE_TOKEN.finditer(sent):
            d = _norm_date(m.group(0))
            # window: 60 chars before, 40 after — the clause that assigns the role
            lo = max(0, m.start() - 60)
            win = sent[lo:m.end() + 40].lower()
            best, best_dist = None, 10**6
            for role, pat in _ROLE_VERBS.items():
                for vm in re.finditer(pat, win):
                    dist = abs((m.start() - lo) - vm.start())
                    if dist < best_dist:
                        best, best_dist = role, dist
            if best:
                roles.setdefault(d, {}).setdefault(best, 0)
                roles[d][best] += 1
    return roles


def _dominant_roles(counts: dict) -> set:
    """Roles the record actually supports for a date (>=25% of its mentions)."""
    if not counts:
        return set()
    top = max(counts.values())
    return {r for r, c in counts.items() if c >= max(2, top * 0.25) or c == top}


def _audit_date_roles(slug: str, block: str, cf_text: str) -> list:
    """Flag sentences that attach a date to a role the record assigns elsewhere."""
    roles = _date_roles_from_record(cf_text)
    if not roles:
        return []
    problems = []
    for sent in _split_sents(block or ""):
        for dm in _DATE_TOKEN.finditer(sent):
            d = dm.group(0)
            # PERIOD BOUNDARY: "from D1 through/to D2" states a RANGE. Its endpoints
            # carry no event role, so the role verb in the clause ("paid") does not
            # describe what happened ON those dates.
            _pre = sent[max(0, dm.start() - 24):dm.start()].lower()
            _post = sent[dm.end():dm.end() + 24].lower()
            if re.search(r"\b(?:from|between|since|beginning|starting)\s*$", _pre) or \
               re.search(r"^\s*,?\s*(?:through|thru|to|until|till|and)\b", _post):
                continue
            _counts = roles.get(_norm_date(d), {})
            known = _dominant_roles(_counts)
            if not known:
                continue
            # every role the record EVER attaches to this date, not just the dominant
            # one: a date can legitimately carry a secondary role in the same sentence.
            supported = set(_counts)
            # SAME WINDOW AS LEARNING: only the role verb closest to THIS date assigns
            # its role. Scanning the whole sentence flagged every unrelated verb in a
            # multi-date sentence ("paying premiums through Nov 2025 and providing proof
            # on Feb 16 2026" charged Feb 16 with 'pay').
            lo = max(0, dm.start() - 60)
            win = sent[lo:dm.end() + 40].lower()
            scored = []
            _dpos = dm.start() - lo
            for role, pat in _ROLE_VERBS.items():
                for vm in re.finditer(pat, win):
                    _dist = abs(_dpos - vm.start())
                    # A role verb far from the date, or separated from it by a clause
                    # boundary, does not describe that date. "Signed ... on March 21,
                    # 2019; Sept 23, 2022; and Nov 25, 2025, authorizing ... to debit"
                    # states signing dates, not payment dates.
                    if _dist > 45:
                        continue
                    _lo2, _hi2 = sorted((_dpos, vm.start()))
                    _between = win[_lo2:_hi2]
                    if re.search(r"[;:]|\)\s|\.\s", _between):
                        continue
                    # markdown list-item boundary = clause boundary: the verb belongs to
                    # a DIFFERENT bullet and says nothing about this bullet's date
                    if re.search(r"(?:^|\s)[-*+]\s|\s\d{1,2}\.\s", _between):
                        continue
                    scored.append((_dist, role))
            if not scored:
                continue
            scored.sort()
            best_dist, best = scored[0]
            # AMBIGUITY GUARD: a sentence narrating two events ("after selling ... on D1,
            # and after Defendant cancelled ... on D2") puts two role verbs almost equally
            # near D1. A near-tie is not evidence of misuse — say nothing rather than
            # accuse the analysis of an error it did not make.
            rival = next((d for d, r in scored if r != best), None)
            if rival is not None and (rival - best_dist) <= 12:
                continue
            # a correct role among the runners-up also clears it: the sentence used the
            # date correctly and merely mentions another event nearby.
            if any(r in known for _d, r in scored[:3]):
                continue
            if best in supported:
                continue
            if best and best not in known:
                problems.append(
                    f"date {d} used as '{best}' but the record assigns it "
                    f"{sorted(known)}: " + " ".join(sent.split())[:110])
    return problems


def _audit_analysis_coverage(slug: str, name: str, block: str, ctx: str) -> list:
    """Did the analysis WRITE what the record and the verified authority put in front of it?

    Prompt rules alone did not land these: the spine's controlling cases reached m0's context
    and never appeared in its output, so the drafter could not cite what the court never saw.
    Deterministic, advisory — prints what was dropped so the gap is visible rather than silent.
    """
    if name != "rebuttal" or not block:
        return []
    gaps = []
    missing = [c for c in _spine_authorities_in_context(ctx)
               if not _cite_satisfied(c, block)]
    if missing:
        gaps.append(f"verified authority in context but NOT in the analysis: {', '.join(missing[:6])}")
    # record anchors: dates/amounts the case's own evidence declares
    anchors = set()
    for p in sorted((CASES / slug / "drafts").rglob("*.md")) if (CASES / slug / "drafts").is_dir() else []:
        if "_DRAFT" in p.name or ".bak" in p.name:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        seg = re.search(r"^##\s*NEW EVIDENCE BLOCK(.*?)(?=^##\s|\Z)", t, re.S | re.M)
        if not seg:
            continue
        anchors |= set(re.findall(r"\b\d{2}/\d{2}/\d{4}\b", seg.group(1)))
        anchors |= set(re.findall(r"\$[\d,]+\.\d{2}", seg.group(1)))
        if anchors:
            break
    miss_a = [a for a in sorted(anchors) if a not in block][:6]
    if anchors and len(miss_a) == len(anchors):
        gaps.append(f"NO record anchor (date/amount) from the evidence register appears in the "
                    f"analysis — it argues law without the facts (e.g. {', '.join(miss_a[:3])})")
    return gaps


_COUNT_HEAD = re.compile(
    r"^\s*(?:\*\*)?COUNT\s+([IVXL]+)\s*[-\u2013\u2014:]+\s*(.+?)(?:\*\*)?\s*$",
    re.M | re.I)

# How an adverse filing names one of our counts, e.g.
#   "Count III of the Complaint asserts a claim for common law bad faith"
#   "Plaintiff's claims for unjust enrichment (Count II) and conversion (Count VI) are
#    tort actions"
_THEIR_LABEL = [
    re.compile(r"Count\s+([IVXL]+)\s+of\s+the\s+Complaint\s+(?:purports\s+to\s+)?"
               r"assert[s]?\s+(?:a\s+)?claim[s]?\s+(?:for|of)\s+(.{3,70}?)[\.;,]", re.I | re.S),
    re.compile(r"(?:claim|action)[s]?\s+for\s+(.{3,60}?)\s*\(Count\s+([IVXL]+)\)", re.I | re.S),
]

_NOISE = re.compile(r"\s+")


def _norm_label(t: str) -> str:
    t = _NOISE.sub(" ", (t or "")).strip().lower()
    t = re.sub(r"^(?:a|an|the)\s+", "", t)
    t = re.sub(r"[\u201c\u201d\"\'\.]+", "", t)
    return t.strip(" -\u2013\u2014:")


def _our_counts(slug: str) -> dict:
    """Count numeral -> the heading OUR complaint actually uses."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return {}
    best, best_n = {}, 0
    cands = [q for q in d.rglob("*")
             if q.is_file() and q.suffix.lower() in (".txt", ".md")
             and ("complaint" in q.name.lower() or "petition" in q.name.lower())]
    for p in cands:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found = {}
        for m in _COUNT_HEAD.finditer(txt):
            num = m.group(1).upper()
            head = _NOISE.sub(" ", m.group(2)).strip(" -\u2013\u2014*")
            # headings wrap; pull the continuation line when it is still all caps
            tail = txt[m.end():m.end() + 120].split("\n")
            if tail and tail[0].strip() and tail[0].strip() == tail[0].strip().upper() \
                    and len(tail[0].strip()) > 3 and not tail[0].strip()[0].isdigit():
                head = (head + " " + tail[0].strip()).strip()
            found.setdefault(num, head)
        if len(found) > best_n:
            best, best_n = found, len(found)
    return best


def _their_labels(slug: str) -> dict:
    """Count numeral -> how the ADVERSE filing names that count."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return {}
    out = {}
    newest = sorted(d.rglob("*.txt"), key=lambda q: q.stat().st_mtime, reverse=True)
    for p in newest[:40]:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "COUNT I " in txt.upper() and "WHEREFORE" in txt.upper() and \
                "objection" not in txt.lower():
            continue  # this is our own complaint, not their filing
        for rx in _THEIR_LABEL:
            for m in rx.finditer(txt):
                g1, g2 = m.group(1), m.group(2)
                num, lab = (g1, g2) if re.fullmatch(r"[IVXL]+", g1 or "", re.I) else (g2, g1)
                if not num or not lab:
                    continue
                num = num.upper()
                lab = _NOISE.sub(" ", lab).strip()
                if 3 < len(lab) < 90:
                    out.setdefault(num, lab)
    return out


def _mischaracterizations(slug: str) -> list:
    """Counts the adverse filing renames. Returns (numeral, ours, theirs) triples."""
    ours, theirs = _our_counts(slug), _their_labels(slug)
    if not ours or not theirs:
        return []
    hits = []
    for num, tl in theirs.items():
        oh = ours.get(num)
        if not oh:
            continue
        a, b = _norm_label(oh), _norm_label(tl)
        if not a or not b:
            continue
        # same claim, just shorter phrasing -> not a mischaracterization
        if a in b or b in a:
            continue
        aw = {w for w in re.split(r"[^a-z]+", a) if len(w) > 3}
        bw = {w for w in re.split(r"[^a-z]+", b) if len(w) > 3}
        if aw and bw and len(aw & bw) / max(1, len(aw | bw)) >= 0.6:
            continue
        hits.append((num, oh, tl))
    return sorted(hits, key=lambda t: len(t[0]))



def _relabel_block(slug: str) -> str:
    """The counts the adverse filing renames, quoted from both documents."""
    mis = _mischaracterizations(slug)
    if not mis:
        return ""
    rows = []
    for num, ours, theirs in mis:
        rows.append(f"  - Count {num}: THE COMPLAINT PLEADS \"{ours}\". THE MOVANT "
                    f"CALLS IT \"{theirs}\" and demurs to THAT.")
    return (
        " COUNTS THE MOVANT RENAMED (answer these FIRST, before any merits argument): "
        + " ".join(rows) +
        " For EACH: state the heading as pleaded, state the label they substituted, and "
        "say plainly that the objection is addressed to a claim the Complaint does not "
        "contain, so it is non-responsive and MOOT for want of a pleaded target. Do NOT "
        "argue in the alternative, do NOT offer to reframe or replead, and do NOT repeat "
        "their label as if it were ours anywhere in the answer -- a demurrer must take "
        "the pleading as written. Where the movant's own filing concedes the character of "
        "the claim (e.g. an introduction stating the allegations sound in breach of "
        "contract), quote that concession against them. THEN answer the objection they "
        "would have had if they had read the count correctly. ")


def _adopts_their_label(slug: str, block: str) -> list:
    """Places where the analysis repeats the movant's relabel as if it were ours."""
    out = []
    low = (block or "").lower()
    for num, ours, theirs in _mischaracterizations(slug):
        t = _norm_label(theirs)
        if not t or t not in low:
            continue
        # quoting it to reject it is fine; asserting it as our claim is not
        # Cues that the label is being ATTRIBUTED to the movant or refused, not adopted.
        ok_ctx = ("does not contain", "not contain", "not pleaded", "not plead",
                  "never pleaded", "mischaracter", "recharacter", "relabel", "renamed",
                  "the movant calls", "movant calls it", "they call it", "calls it",
                  "demurs to that", "demurred to", "attack a claim", "attacks a claim",
                  "non-responsive", "nonresponsive", "no such count", "as if it were",
                  "objection is addressed to", "attacked counts", "they characterize",
                  "recast", "substituted", "label")
        j, adopted = 0, False
        while True:
            j = low.find(t, j)
            if j < 0:
                break
            # a label in quotation marks is attributed, not asserted as ours
            pre_q = low[max(0, j - 2): j]
            post_q = low[j + len(t): j + len(t) + 2]
            quoted = any(q in pre_q for q in ('"', "\u201c", "'")) or \
                     any(q in post_q for q in ('"', "\u201d", "'"))
            # wider window: the disclaimer often trails the label by a full clause
            ctx = low[max(0, j - 400): j + 400]
            # the line itself: an inventory of THEIR attacks quotes their labels
            ls = low.rfind("\n", 0, j) + 1
            le = low.find("\n", j)
            line = low[ls: le if le > 0 else len(low)]
            inventory = any(k in line for k in ("attacked count", "counts attacked",
                                                "their objection", "objections:",
                                                "movant", "they "))
            if not quoted and not inventory and not any(k in ctx for k in ok_ctx):
                adopted = True
                break
            j += len(t)
        if adopted:
            out.append((num, ours, theirs))
    return out


def _prior_doc(cf_path) -> str:
    """CASE_FACTS as it stands on disk -- lets a fold see a previous run's output."""
    try:
        return Path(cf_path).read_text(encoding="utf-8")
    except Exception:
        return ""




def _prior_section(doc: str, header: str) -> str:
    """The `header` block as it stands on disk (header -> next top-level '## ')."""
    if not doc:
        return ""
    lines = doc.splitlines(keepends=True)
    _h = header.strip()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == _h or ln.strip().startswith(_h + " \u2014")
                  or ln.strip().startswith(_h + " (")), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "".join(lines[start:end])


def _subtree(doc: str, heading: str) -> str:
    """The '### <heading>' subtree, up to the next heading of any level."""
    if not doc:
        return ""
    lines = doc.splitlines(keepends=True)
    h = heading.strip().lower()
    start = next((i for i, ln in enumerate(lines)
                  if ln.lstrip().startswith("#")
                  and ln.strip().lstrip("#").strip().lower().rstrip(":") == h), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].startswith("## ") or lines[j].startswith("### ")), len(lines))
    return "".join(lines[start:end]).rstrip()



_PROFILES_PATH = BASE / "output" / "audit" / "m1_style_profiles.json" if "BASE" in dir() else None


def _movant_filing_contract(court: str = "PA_CP_PHILA",
                            kind: str = "movant_preliminary_objections") -> list:
    """The rule contract the MOVANT'S filing must satisfy, derived from the court's own
    rules by pipeline.rule_requirements. Data, never hardcoded anatomy."""
    import json
    try:
        pf = CASES.parent.parent / "output" / "audit" / "m1_style_profiles.json"
        if not pf.exists():
            pf = (_eroot() / "output/audit/m1_style_profiles.json")
        prof = json.loads(pf.read_text(encoding="utf-8"))
        return (prof.get(court, {}).get(kind, {}) or {}).get("mandatory_sections", []) or []
    except Exception:
        return []


# Evidence that a required item actually accompanied the filing. Each probe is a phrase a
# compliant filing uses; absence of ALL of them is the defect the rule itself sanctions.
_ITEM_PROBES = {
    # Each probe matches how the item ACTUALLY APPEARS in a filing, not how the rule
    # names it. A proposed order is captioned "ORDER" and never "proposed order";
    # probing the rule's own words manufactures a false defect. Verified against the
    # live movant filing before use.
    "*1028(c)(1)(a)": (r"(?i)\bmemorandum of law\b|\bbrief in support\b|\bBRIEF\b",
                       "no brief or memorandum of law"),
    "*1028(c)(1)(b)": (r"(?i)\bexhibit\s+[A-Z0-9]\b|attached hereto|annexed hereto|"
                       r"true and correct cop(?:y|ies)",
                       "no items attached"),
    "*1028(c)(1)(c)": (r"(?im)^\s*(?:proposed\s+)?ORDER\s*$|upon consideration of|"
                       r"it is hereby ORDER",
                       "no proposed order"),
}


def _fold_rule_defects(slug: str, block: str, doc: str = "") -> str:
    """PRODUCER for the 'RULE DEFECTS IN THE MOVANT'S FILING' fold section.

    A defect in the MOVANT'S OWN filing under the court's local rules is a THRESHOLD
    ground -- decided before the merits, and the rule usually carries its own sanction.
    This is computed from (a) the rule contract in the style profile and (b) the movant's
    filing text, so it is agnostic: no case, count, or party is named in the logic."""
    head = "RULE DEFECTS IN THE MOVANT'S FILING"
    if re.search(r"(?im)^\s*#{2,4}\s*" + re.escape(head), block or ""):
        return block
    reqs = _movant_filing_contract()
    if not reqs:
        return block
    adverse = _adverse_packet(slug) or _adverse_text(slug)
    if not adverse or len(adverse) < 400:
        return block
    missing = []
    for r in reqs:
        rule = (r.get("rule") or "").strip()
        probe, label = _ITEM_PROBES.get(rule, (None, None))
        if not probe:
            continue
        if not re.search(probe, adverse):
            missing.append((rule, r.get("heading", ""), label, (r.get("requires") or "").strip()))
    if not missing:
        return block
    lines = [f"### {head}", ""]
    for rule, heading, label, requires in missing:
        lines.append(
            f"- **{label.capitalize()} (Phila.Civ.R. {rule}).** The rule requires "
            f"\u201c{requires}\u201d The movant's filing contains none. "
            f"The rule carries its own sanction: the Court \u201cmay decide preliminary "
            f"objections against a party who fails to attach to the filing those items "
            f"necessary to enable the Court to determine the preliminary objections.\u201d"
        )
    lines.append("")
    print(f"  [{slug}] RULE-DEFECT FOLD: {len(missing)} local-rule defect(s) in the movant's "
          f"filing: {', '.join(m[0] for m in missing)}")
    return (block.rstrip() + "\n\n" + "\n".join(lines)).rstrip() + "\n"


def _fold_assert_sections(slug: str, header: str, block: str, doc: str = "") -> str:
    """FOLD ASSERT -- the section-level twin of EVIDENCE_ASSERT.

    A contracted fold section that a PREVIOUS run earned must not disappear because a
    later LLM rewrite omitted it. Losing one is silent (exit 0, shorter file) and costs
    every lever it carried, so restore it deterministically from the record on disk."""
    if not block:
        return block
    prior = _prior_section(doc, header)
    if not prior:
        return block
    restored = []
    for _fs in FOLD_SECTIONS:
        head = _fs[0]
        if re.search(r"(?im)^\s*#{2,4}\s*" + re.escape(head), block):
            continue
        earned = _subtree(prior, head)
        if not earned:
            continue
        block = block.rstrip() + "\n\n" + earned + "\n"
        restored.append(head)
    if restored:
        print(f"  [{slug}] FOLD_ASSERT: {len(restored)} contracted section(s) the rewrite "
              f"dropped, restored from record: {'; '.join(restored)}")
    return block


def _fold_relabel_answer(slug: str, block: str, doc: str = "") -> str:
    """If the analysis adopted a relabel, prepend the answer it failed to make."""
    bad = _adopts_their_label(slug, block)
    if not bad:
        # not our drift -- but the objection is still non-responsive if the movant
        # demurs to a label the Complaint never pleaded
        bad = _mischaracterizations(slug)
    if not bad:
        return block
    # Idempotent within the block being written. NOT keyed on the prior file: that block
    # is about to be replaced, so a stale copy there must not suppress the rewrite.
    if "NON-RESPONSIVE OBJECTION" in (block or ""):
        return block
    rows = ["### NON-RESPONSIVE OBJECTION — THE MOVANT RENAMED THE COUNT", ""]
    for num, ours, theirs in bad:
        rows.append(
            f"- **Count {num} is pleaded as {ours}.** The objection is addressed to "
            f"\"{theirs}\" — a claim this Complaint does not contain. A demurrer admits "
            f"the pleading as written and must be answered on the count as pleaded; an "
            f"objection aimed at a theory the plaintiff never advanced is non-responsive "
            f"and presents nothing for the Court to sustain. Plaintiff does not replead, "
            f"reframe, or abandon the count as pleaded, and the movant is put to its "
            f"burden to identify BY COUNT AND PARAGRAPH where the theory it attacks "
            f"appears in the Complaint.")
        rows.append("")
    # The block STARTS with its "## ..." section header; prepending above it orphans the
    # header and _current_block then reads an empty section. Insert after the header.
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + rows + lines[1:])
    return "\n".join(rows) + "\n" + body



_WHEREFORE_COUNTS = re.compile(
    r"WHEREFORE.{0,400}?dismiss\s+Count[s]?\s+([IVXL,\s and]+?)(?:\s+of\b|,\s*with|\.)",
    re.I | re.S)


def _counts_they_move_against(slug: str) -> set:
    """Count numerals the adverse filing asks the court to dismiss."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return set()
    out = set()
    for p in sorted(d.rglob("*.txt"), key=lambda q: q.stat().st_mtime, reverse=True)[:40]:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _WHEREFORE_COUNTS.finditer(txt):
            for tok in re.split(r"[,\s]+and\s+|[,\s]+", m.group(1)):
                tok = tok.strip().upper()
                if re.fullmatch(r"[IVXL]+", tok or ""):
                    out.add(tok)
        if out:
            break
    return out


def _adverse_text(slug: str) -> str:
    """The movant's filing text (the one whose WHEREFORE names counts)."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return ""
    for p in sorted(d.rglob("*.txt"), key=lambda q: q.stat().st_mtime, reverse=True)[:40]:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _WHEREFORE_COUNTS.search(txt):
            return txt
    return ""



def _adverse_packet(slug: str) -> str:
    """The movant's filing READ WHOLE.

    A single e-filing is one packet: objections + brief + proposed order + COS, sometimes
    split across documents. `_adverse_text` returns the FIRST matching document and stops,
    so judging the packet by it reports items as missing that were filed in a sibling
    document -- a false accusation in a brief. Documents are ingested into the case record
    precisely so there is no such separation, so read the whole filing, deduped."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return ""
    seen, parts = set(), []
    cands = sorted(d.rglob("*.txt"), key=lambda q: q.stat().st_mtime, reverse=True)[:60]
    anchor = ""
    for q in cands:
        try:
            txt = q.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not _WHEREFORE_COUNTS.search(txt):
            continue
        if not anchor:
            # the docket event this filing belongs to: strip OCR/extract suffixes
            anchor = re.sub(r"(\.pdf|\.doctext|\.txt)+$", "", q.name, flags=re.I)
        key = re.sub(r"\s+", " ", txt).strip()[:400]
        if key in seen:
            continue
        seen.add(key)
        parts.append(txt)
    if not parts:
        return ""
    # pull in siblings of the SAME docket event that carry no WHEREFORE (a standalone
    # proposed order or brief), so a split packet is still read whole.
    if anchor:
        for q in cands:
            base = re.sub(r"(\.pdf|\.doctext|\.txt)+$", "", q.name, flags=re.I)
            if base != anchor:
                continue
            try:
                txt = q.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            key = re.sub(r"\s+", " ", txt).strip()[:400]
            if key in seen:
                continue
            seen.add(key)
            parts.append(txt)
    return "\n\n".join(parts)


_GROUND = re.compile(r"\b\d{2,4}\s+(?:A\.[0-9a-z]+|Pa\.|U\.S\.)|Pa\.R\.C\.P|Pa\.R\.Civ\.P|"
                     r"\b\d+\s+Pa\.C\.S|\bRule\s+\d+", re.I)


def _waivers(slug: str) -> list:
    """(kind, count, detail) for every way the movant failed to answer a count."""
    ours = _our_counts(slug)
    if not ours:
        return []
    targets = _counts_they_move_against(slug)
    adverse = _adverse_text(slug)
    if not adverse:
        return []
    out = []

    # 1) RELABEL -- demurred to a claim we did not plead
    for num, oh, th in _mischaracterizations(slug):
        out.append(("RELABEL", num, (oh, th)))

    relabelled = {n for n, _, _ in _mischaracterizations(slug)}

    # 2/3) for each count they move against, did they reach it, and on what ground?
    for num in sorted(targets, key=len):
        if num in relabelled:
            continue  # already covered, stronger, by the relabel point
        head = ours.get(num)
        if not head:
            continue
        # every passage that names this count
        spans = [m.start() for m in re.finditer(rf"Count\s+{num}\b", adverse, re.I)]
        if not spans:
            out.append(("UNADDRESSED", num, head))
            continue
        grounded = False
        for j in spans:
            if _GROUND.search(adverse[max(0, j - 700): j + 700]):
                grounded = True
                break
        if not grounded:
            out.append(("NO GROUND", num, head))
    return out


# Operative phrases that identify a waiver/consolidation rule in ANY procedural code.
# Probing by the rule's own words (not its number) is what makes this portable: a
# federal Rule 12(g)/(h), a PA Rule 1028(b)/1032(a), or another state's analogue all
# answer to the same operative language.
_WAIVER_RULE_PROBES = [
    ("objections raised at one time",
     ["objections shall be raised at one time",
      "must be raised at one time",
      "shall be joined in a single motion",
      "consolidation of defenses"]),
    ("objections not presented are waived",
     ["waives all defenses and objections which are not presented",
      "waives all defenses and objections which he does not present",
      "party waives any defense listed",
      "failure to raise", ]),
    ("grounds stated specifically",
     ["shall state specifically the grounds relied upon",
      "state with specificity the grounds"]),
]


def _rule_text_for(probe_list, limit: int = 2) -> list:
    """(source, sentence) for the first corpus hits on any probe phrase."""
    try:
        import sqlite3
        con = _corpus_connect()
        cur = con.cursor()
    except CorpusLocked:
        raise
    except Exception:
        return []
    out = []
    for phrase in probe_list:
        if len(out) >= limit:
            break
        try:
            cur.execute("SELECT source, text FROM chunk_text WHERE text LIKE ? LIMIT 3",
                        ("%" + phrase + "%",))
            rows = cur.fetchall()
        except Exception:
            continue
        for src, t in rows:
            flat = " ".join((t or "").split())
            i = flat.lower().find(phrase.lower())
            if i < 0:
                continue
            # expand around the PHRASE to its own sentence boundaries
            lo = flat.rfind(". ", 0, i)
            lo = 0 if lo < 0 else lo + 2
            hi = flat.find(". ", i + len(phrase))
            hi = len(flat) if hi < 0 else hi + 1
            sent = flat[lo:hi].strip()
            # a caption is a short title-case fragment with no verb -- not the rule text
            if phrase.lower() not in sent.lower() or len(sent) < 40:
                sent = flat[max(0, i - 40): i + 260].strip()
            # keep it to a quotable length
            if len(sent) > 420:
                sent = sent[:420].rsplit(" ", 1)[0] + "..."
            out.append((src or "corpus", sent))
            break
    try:
        con.close()
    except Exception:
        pass
    return out[:limit]


def _waiver_authority_block() -> str:
    """The enacted waiver/one-time rule text, quoted from corpus, or an honest gap."""
    lines, found_any = [], False
    for label, probes in _WAIVER_RULE_PROBES:
        hits = _rule_text_for(probes, limit=1)
        if hits:
            found_any = True
            src, sent = hits[0]
            lines.append(f"  - {label} — VERBATIM [corpus:{src}]: \"{sent}\"")
        else:
            lines.append(f"  - {label} — NOT IN CORPUS: do not paraphrase a rule whose "
                         f"text we do not hold; argue the waiver from the movant's own "
                         f"omission and the record instead.")
    if not found_any:
        return ""
    return (" ENACTED WAIVER RULE TEXT (quote it, do not paraphrase): "
            + " ".join(lines) + " ")


def _waiver_block(slug: str) -> str:
    """Hand m0 the waivers, computed from their own filing."""
    w = _waivers(slug)
    if not w:
        return ""
    rows = []
    for kind, num, detail in w:
        if kind == "RELABEL":
            oh, th = detail
            rows.append(f"  - Count {num} [RELABEL]: they demurred to \"{th}\" while the "
                        f"Complaint pleads \"{oh}\" — the count AS PLEADED drew no "
                        f"objection at all.")
        elif kind == "UNADDRESSED":
            rows.append(f"  - Count {num} [UNADDRESSED]: their WHEREFORE asks dismissal of "
                        f"this count, but their argument never reaches it.")
        else:
            rows.append(f"  - Count {num} [NO GROUND]: they attack it without citing any "
                        f"rule, statute, or decision directed to that count.")
    return (
        " WAIVER — OBJECTIONS THEY DID NOT MAKE: " + " ".join(rows) +
        " For EACH, argue the WAIVER affirmatively, not as a complaint about their brief: "
        "preliminary objections must be raised at one time and with specificity, and an "
        "objection not raised as to the count AS PLEADED is not preserved. State that the "
        "movant, having elected to attack a claim the Complaint does not contain (or having "
        "named a count in its prayer without reaching it), has waived any FURTHER PRELIMINARY "
        "OBJECTION to that count as pleaded and cannot cure by reply brief, at argument, or "
        "by later preliminary objection — a party may not raise seriatim objections, and one "
        "who invokes waiver against a pleader is held to the same standard (no "
        "approbate-and-reprobate; equality is equity). SCOPE IS THE RULE'S, NEVER BROADER: "
        "Pa.R.C.P. 1032(a) preserves failure to state a claim, failure to join an "
        "indispensable party, and the (a)(7)/(a)(8) defenses regardless of waiver, so never "
        "write that the movant waived 'any objection' or 'all objections' — write exactly what "
        "1028(b)/1032(a) forecloses (a second set of preliminary objections; the waivable "
        "(a)(1)/(a)(3)/(a)(6) grounds) and nothing more. Demand that the count proceed to "
        "answer on the merits. "
        "Quote the enacted rule text supplied below VERBATIM for every waiver statement; "
        "where it says NOT IN CORPUS, argue the waiver from the movant's own omission and "
        "the record rather than paraphrasing a rule we do not hold. "
        + _waiver_authority_block())



_PULL_MISSING = True   # NOT IN CORPUS -> fetch from CAP/CourtListener
_CITE_RX = re.compile(r"\b\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.(?:\s?Super\.)?|U\.S\.|S\.Ct\.)\s+\d+\b")
# Party names contain commas ("CoreStates Bank, N.A. v. Cutillo"), so a comma is not a
# reliable left boundary. Anchor on " v. " and walk outward across limited segments.
_V_RX = re.compile(r"\s+v\.?\s+", re.I)


def _name_before_cite(pre: str) -> str:
    """Recover the full case name ending just before a citation.

    Token-based on purpose: "CoreStates Bank, N.A. v. Cutillo" carries a comma INSIDE
    the party name while the surrounding sentence carries commas of its own, so comma
    splitting cannot find the boundary. Walk outward from " v. " and stop at the first
    token that cannot be part of a party name.
    """
    _SUFFIX = re.compile(r"^(?:N\.?A\.?|Inc\.?|L\.?L\.?C\.?|L\.?L\.?P\.?|L\.?P\.?|"
                         r"Co\.?|Corp\.?|Ltd\.?|P\.?C\.?|Ass'?n\.?|Bros\.?|"
                         r"Cas\.?|Sur\.?|Ins\.?|Mut\.?|Nat'?l\.?|Metro\.?|Fire|Life)[,.]?$",
                         re.I)
    _STOP = {"the", "in", "see", "under", "of", "rule", "accord", "cf", "eg", "e.g",
             "citing", "quoting", "per", "compare", "but", "also", "id", "and", "at",
             "from", "by", "for", "held", "that", "supra", "infra"}

    def _party_tok(tok: str) -> bool:
        bare = tok.strip("(),;:.")
        if not bare:
            return False
        if _SUFFIX.match(tok):
            return True
        if bare == "&":
            return True
        if bare.lower() in _STOP:
            return False
        return bare[:1].isupper()

    pre = pre.rstrip().rstrip(",")
    mv = None
    for mv in _V_RX.finditer(pre):
        pass
    if not mv:
        return ""
    left_toks = pre[:mv.start()].split()
    right_toks = pre[mv.end():].split()

    # LEFT: walk backwards while the token can belong to a party name
    lt = []
    for tok in reversed(left_toks):
        if not _party_tok(tok):
            break
        lt.insert(0, tok)
        if len(lt) >= 8:
            break
    # RIGHT: walk forwards, stopping at the citation or a non-party token
    rt = []
    for tok in right_toks:
        if re.match(r"^\d", tok) or not _party_tok(tok):
            break
        rt.append(tok)
        if tok.endswith(",") or len(rt) >= 8:
            break

    lname = " ".join(lt).strip().rstrip(",")
    rname = " ".join(rt).strip().rstrip(",")
    if not lname or not rname:
        return ""
    return f"{lname} v. {rname}"

def _our_pleaded_authorities(slug: str) -> dict:
    """cite -> case name, for authorities OUR complaint pleads. These are ours."""
    d = CASES / slug / "intake"
    if not d.is_dir():
        return {}
    out = {}
    cands = [q for q in d.rglob("*")
             if q.is_file() and q.suffix.lower() in (".txt", ".md")
             and ("complaint" in q.name.lower() or "petition" in q.name.lower())]
    for p in cands:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        flat = " ".join(txt.split())
        for m in _CITE_RX.finditer(flat):
            cite = m.group(0)
            pre = flat[max(0, m.start() - 130): m.start()]
            nm = _name_before_cite(pre)
            if nm:
                out.setdefault(cite, nm)
    return out


def _corpus_rule_sentence(cite: str, name: str = "") -> str:
    """A sentence from corpus stating what THIS authority holds. '' when none is clean.

    A wrong rule is worse than none: v1 attached a Pa.R.Civ.P. rule-table fragment to
    Bruno and a UTPCPL sentence to Mitchell. Score on proximity + proposition verbs and
    reject index/table/citation-list boilerplate outright.
    """
    _BOILER = ("pa.r.civ.p", "pa.r.c.p", "effdateamd", "amendatory", "repealed",
               "table of", "index", "see also", "cross reference", "editor's note",
               "historical note", "suggested", "note:", "supp.", "et seq.")
    _VERB = ("requires", "require", "elements of", "must show", "must prove", "holds",
             "held that", "is imposed", "establishes", "establish", "consists of",
             "sounds in", "is barred", "bars ", "does not apply", "applies only",
             "constitutes", "means that", "is defined", "the test is", "a plaintiff must")
    # procedural history is not a holding: a later opinion's "this Court granted X's petition
    # for allowance of appeal" sits next to the cite and scored as Terletsky's rule.
    _HISTORY = ("petition for allowance", "allowance of appeal", "granted allocatur", "we granted",
                "subsequently granted", "remanded for", "vacated the", "appeal was taken", "appealed to",
                "this court subsequently", "limited to the following question", "as phrased by")
    name = re.sub(r"[\u2070-\u209f\u00b2\u00b3\u00b9,]+(?=\s|$)", "", name or "").strip()   # OCR footnote superscripts
    rows = []
    try:
        import sqlite3
        # 1. the authority's OWN opinion first (majority text), so a citing case never speaks for it
        _own = _authority_own_text(cite, name)
        if _own:
            rows = [(_own,)]
            cite_probe = cite
        if not rows:
            con = _corpus_connect()
            cur = con.cursor()
            cur.execute("SELECT text FROM chunk_text WHERE text LIKE ? LIMIT 8",
                        ("%" + cite + "%",))
            rows = cur.fetchall()
            con.close()
    except CorpusLocked:
        raise
    except Exception:
        return ""
    # a short distinctive token from the case name, to confirm the sentence is on-topic
    key = ""
    if name:
        for w in re.split(r"[^A-Za-z]+", name):
            if len(w) > 4 and w.lower() not in ("bank", "insurance", "company", "corp",
                                                "national", "mutual", "casualty"):
                key = w.lower()
                break
    best, best_score = "", 0.0
    for (t,) in rows:
        flat = " ".join((t or "").split())
        i = flat.find(cite)
        if i < 0 and _own:
            i = 0; seg = flat                      # own opinion: the whole holding text is in scope
        elif i < 0:
            continue
        else:
            seg = flat[max(0, i - 700): i + 400]
        for m in re.finditer(r"[^.;]{50,360}[.;]", seg):
            sent = m.group(0).strip()
            low = sent.lower()
            if any(b in low for b in _BOILER) or any(h in low for h in _HISTORY):
                continue
            # a sentence that is mostly citations is an index line, not a rule
            if len(re.findall(r"\d{2,4}\s+[A-Z]", sent)) > 2:
                continue
            if not any(v in low for v in _VERB):
                continue
            score = 1.0
            # closer to the cite is more likely to be about it
            dist = abs((max(0, i - 700) + m.start()) - i)
            score += max(0.0, 2.0 - dist / 350.0)
            _rule_form = re.search(r"\b(must (show|prove|establish|demonstrate|plead)|requires?\b|elements? of|the test\b|"
                                   r"a plaintiff\b|an insurer\b|an insured\b|is (defined|barred|required)|applies (only|to)|"
                                   r"does not apply|cause of action for|to recover\b|to state a claim)", low)
            if _own:
                # own opinion: a sentence naming the party or quoting the record is APPLICATION, not the rule
                score += 2.0 + (2.0 if _rule_form else 0.0)
                if key and key in low: score -= 1.5
                if "[" in sent or re.search(r"\b(19|20)\d\d\b", sent): score -= 1.5
                if re.search(r"\bwe (hold|conclude|held)\b", low): score += 1.0
            elif key and key in low:
                score += 2.0
            if 80 <= len(sent) <= 260:
                score += 0.5
            if score > best_score:
                best, best_score = sent, score
    # require a real signal, not just any verb match
    return best if best_score >= 2.0 else ""


def _pull_missing_authority(cite: str, name: str) -> bool:
    """Not in corpus -> pull it (CAP static bulk / CourtListener), then re-probe.

    pipeline.case_fetch is the repaired puller: CAP(given) -> CAP(parallel cite) ->
    Wikisource -> authenticated CourtListener, name-guarded against citing-case
    poisoning. Reuse it; never hand-roll a fetch here.
    """
    if not name:
        return False
    try:
        from pipeline.case_fetch import ingest_case
    except Exception:
        return False
    try:
        res = ingest_case(name, cite)
        return bool(res)
    except Exception:
        return False


def _their_authorities(slug: str) -> dict:
    """cite -> case name, for every authority the MOVANT cites."""
    txt = _adverse_text(slug)
    if not txt:
        return {}
    out = {}
    flat = " ".join(txt.split())
    for m in _CITE_RX.finditer(flat):
        cite = m.group(0)
        win = flat[max(0, m.start() - 150): m.start()]
        # a quoted case name from the PREVIOUS sentence is not this cite's name
        for q in ("\u201d", "\u2019\u201d", '"'):
            k = win.rfind(q)
            if k >= 0 and k > len(win) - 60:
                win = win[k + len(q):]
        nm = _name_before_cite(win)
        if not nm:
            # they cite by name in one sentence and bare-cite in the next: reach back
            wide = flat[max(0, m.start() - 700): m.start()]
            cands = [mm for mm in _V_RX.finditer(wide)]
            if cands:
                tail = wide[: cands[-1].end() + 60]
                nm = _name_before_cite(tail)
        out.setdefault(cite, nm)
    # federal district / WL cites the movant leans on are authority too
    for m in re.finditer(r"\b\d{3,4}\s+F\.\s?Supp\.\s?\d[a-z]?\s+\d+\b", flat):
        out.setdefault(m.group(0),
                       _name_before_cite(flat[max(0, m.start() - 150): m.start()]))
    return out


def _required_authorities(slug: str, block: str, ctx: str) -> list:
    """(cite, name, rule) the analysis must carry but dropped."""
    need = {}
    for c in _spine_authorities_in_context(ctx or ""):
        need.setdefault(c, "")
    targets = _counts_they_move_against(slug)
    if targets:
        for cite, nm in _our_pleaded_authorities(slug).items():
            need.setdefault(cite, nm)
    # THEIRS: an authority the movant put in issue and we never answer reads as conceded.
    for cite, nm in _their_authorities(slug).items():
        need.setdefault(cite, nm)
    out = []
    for cite, nm in need.items():
        if _cite_satisfied(cite, block or ""):
            continue
        rule = _corpus_rule_sentence(cite, nm)
        if not rule and _PULL_MISSING:
            # NOT IN CORPUS is a finding, not a stopping point: pull it.
            if _pull_missing_authority(cite, nm):
                rule = _corpus_rule_sentence(cite, nm)
                print(f"  [authority] PULLED {nm or ''} {cite} into corpus"
                      + ("" if rule else " (ingested; no clean rule sentence yet)"))
        out.append((cite, nm, rule))
    return out


def _fold_authorities(slug: str, block: str, ctx: str, doc: str = "") -> str:
    """Append the verified authority the analysis dropped, with its rule."""
    miss = _required_authorities(slug, block, ctx)
    if not miss:
        return block
    if "AUTHORITY THE ANALYSIS MUST CARRY" in (block or ""):
        return block
    theirs = _their_authorities(slug)
    ours = _our_pleaded_authorities(slug)
    rows = ["", "### AUTHORITY THE ANALYSIS MUST CARRY", "",
            "_Every authority below must be ANSWERED in the opposition. An authority the "
            "movant placed in issue that we do not address is taken as conceded; an "
            "authority our own Complaint pleads that we drop abandons the count it "
            "supports._", ""]
    for cite, nm, rule in miss:
        head = f"- **{nm}, {cite}**" if nm else f"- **{cite}**"
        # Whose authority it is, stated as a FACT about the record -- not an instruction.
        if cite in theirs and cite not in ours:
            whose = ("The movant relies on this authority; it is confined to its holding "
                     "and the movant has not shown its rule reaches the claims pleaded.")
        elif cite in ours:
            whose = ("Pleaded in the Complaint; it states the elements of the count it "
                     "supports and the movant does not address it.")
        else:
            whose = "Binding authority bearing on the objections before the Court."
        # An authority pulled into corpus during THIS run had no rule when `miss` was
        # computed. Ask again now.
        if not rule:
            rule = _corpus_rule_sentence(cite, nm)
        # FORM GATE: a fragment or a wrong-subject sentence is worse than none -- it
        # renders as one authority narrating another's appeal. Drop it, then ask the
        # LLM to derive the holding from this authority's OWN text (gate validates).
        if rule:
            _ok, _why = _holding_form_ok(rule, cite, nm)
            if not _ok:
                print(f"  [holding] rejected for {cite}: {_why}")
                rule = ""
        if not rule:
            rule = _holding_sentence_llm(cite, nm)
            if rule:
                print(f"  [holding] derived for {cite}")
        if rule:
            rows.append(f"{head} — {rule} {whose}")
        else:
            # *210(4) requires a holding beside every authority. Say plainly that the
            # holding is not in corpus rather than emitting a bare cite.
            rows.append(f"{head} — [holding not in corpus — PULL BEFORE FILING] {whose}")
        rows.append("")
    return (block or "") + "\n" + "\n".join(rows)



# Rules on binding weight, phrased as courts actually phrase them. These are retrieval
# targets against the corpus -- the QUOTE that comes back is the authority, not this list.

# --- holding-sentence form gate + LLM derivation -----------------------------
# Three real defect shapes seen in the the insurer PO run, all rendering as
# "bare cite + boilerplate" in AUTHORITY THE ANALYSIS MUST CARRY:
#   (a) EMPTY      -- Gregg 245 A.3d 637, Mallory 600 U.S. 122: no sentence at all.
#   (b) FRAGMENT   -- Schwartz 932 A.2d 885: "2007) (noting that ..." (mid-parenthetical).
#   (c) WRONG-SUBJECT -- Terletsky 649 A.2d 680 carrying *Rancosky's* procedural history,
#       so the point reads as one case narrating another's appeal.
# A bare cite is not merely thin: under Phila.Civ.R. *210(4) an authority must be
# "immediately preceded or followed by its relevant holding," so an empty holding
# slot is a RULE violation in this forum, not a style blemish.

_HOLDING_MINW = 8

def _holding_form_ok(sent: str, cite: str = "", name: str = "") -> tuple:
    """(ok, reason). Reject fragments and wrong-subject sentences deterministically."""
    s = " ".join((sent or "").split())
    if not s:
        return (False, "empty")
    if len(s.split()) < _HOLDING_MINW:
        return (False, "too short")
    # (b) fragment: opens mid-parenthetical / mid-citation, or unbalanced parens
    if re.match(r"^\s*[\)\];,]", s) or re.match(r"^\s*\d{4}\)", s):
        return (False, "opens mid-citation")
    if s.count(")") > s.count("(") :
        return (False, "unbalanced parenthesis")
    if not re.match(r"^[A-Z\u201c\"\u00b6\d]", s):
        return (False, "does not open a sentence")
    # (d) TRUNCATED ENUMERATION -- Terletsky's two-part bad-faith test arrived as
    #     "(1) that the insurer lacked a reasonable basis for denying benefits;" with
    #     element (2) (knowledge or reckless disregard) severed. Quoting half a
    #     conjunctive test UNDERSTATES our own burden and reads as though the movant's
    #     lesser showing suffices. An opened enumeration must close.
    if "(1)" in s and "(2)" not in s:
        return (False, "enumeration opens at (1) and never reaches (2)")
    # An enumeration whose last element trails off on a separator OR a dangling
    # conjunction ("(1) a duty, (2) a breach, and") was cut before its final element.
    _tail = s.rstrip().rstrip(".")
    for _n in range(2, 7):
        if f"({_n})" in s and f"({_n + 1})" not in s and (
                _tail.endswith((";", ",")) or re.search(r"\b(?:and|or)$", _tail, re.I)):
            return (False, f"enumeration abandoned after ({_n})")
    # A holding ending on a list separator or dangling conjunction is a cut.
    if s.rstrip().endswith((";", ",", ":")):
        return (False, "ends on a list separator")
    if re.search(r"\b(?:and|or|including|namely|such as)\.?$", _tail, re.I):
        return (False, "ends on a dangling conjunction")
    # (c) wrong subject: names a DIFFERENT case as the actor
    #     ("Rancosky claims that the trial court erred ...") under Terletsky's cite.
    own = set()
    for w in re.split(r"[^A-Za-z]+", name or ""):
        if len(w) > 4:
            own.add(w.lower())
    m = re.match(r"^([A-Z][a-z]{4,})\s+(?:claims?|argues?|asserts?|contends?|appeals?|alleges?)\b", s)
    if m and m.group(1).lower() not in own:
        return (False, f"subject is {m.group(1)}, not the cited authority")
    return (True, "ok")


_HOLDING_PROMPT = """You are reading the TEXT OF ONE OPINION to state what it HOLDS.

Write ONE sentence, in the present tense, stating the rule or holding this authority stands for.
Rules:
- Use ONLY the retrieved text. If it does not state a holding, output NONE.
- State the RULE, not the procedural history. Never write "X claims", "X appeals", "the trial court erred".
- Do not name the parties as actors; state the proposition of law.
- No citation, no "Id.", no parentheticals, no quotation marks.
- One sentence, 12-45 words.

Output ONLY compact JSON: {"holding":"<one sentence or NONE>"}

AUTHORITY: {auth}

RETRIEVED OPINION TEXT:
{grounding}
"""

_HOLDING_SIGNALS = (
    "we hold", "we conclude", "we agree", "holds that", "held that", "the rule is",
    "must establish", "must show", "must prove", "elements of", "applies to",
    "does not apply", "is barred", "bars ", "requires that", "we therefore",
    "accordingly, we", "as a matter of law", "the trial court correctly",
)


def _authority_own_text(cite: str, name: str = "", limit: int = 6000) -> str:
    """Text from the authority's OWN opinion, holding passages first.

    `_holding_text` orders by rowid, so for an opinion it returns the caption and
    procedural history and never reaches the holding -- three authorities graded
    NONE on caption text alone. It also matches `%cite%`, which returns CITING
    cases (Wenk's own text is in corpus, but the cite probe found cases quoting it).
    Resolve the SOURCE KEY first, then score that source's chunks for holding language.
    """
    import sqlite3 as _sq
    try:
        con = _corpus_connect()
    except CorpusLocked:
        raise
    except Exception:
        return ""
    cur = con.cursor()
    # 1. the authority's own source key, from the reporter triple in the key itself
    m = re.match(r"\s*(\d+)\s+([A-Za-z\.\d]+)\s+(\d+)", cite or "")
    src = ""
    if m:
        vol, rep, page = m.group(1), re.sub(r"[^a-z0-9]", "", m.group(2).lower()), m.group(3)
        try:
            cur.execute("SELECT DISTINCT source FROM chunk_text WHERE source LIKE ? LIMIT 4",
                        (f"%{vol}{rep}{page}%",))
            hits = [r[0] for r in cur.fetchall()]
            if not hits and name:
                tok = [w.lower() for w in re.split(r"[^A-Za-z]+", name) if len(w) > 4]
                if tok:
                    cur.execute("SELECT DISTINCT source FROM chunk_text "
                                "WHERE source LIKE ? AND source LIKE ? LIMIT 4",
                                (f"%{tok[0]}%", f"%{vol}{rep}{page}%"))
                    hits = [r[0] for r in cur.fetchall()]
            if hits:
                src = hits[0]
        except Exception:
            src = ""
    if not src:
        con.close()
        return ""
    # 2. EVERY chunk of that source, scored for holding language
    try:
        cur.execute("SELECT text FROM chunk_text WHERE source = ?", (src,))
        rows = [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception:
        rows = []
    con.close()
    if not rows:
        return ""
    scored = []
    for t in rows:
        t = " ".join(t.split())
        low = t.lower()
        sc = sum(low.count(k) for k in _HOLDING_SIGNALS)
        # caption/docket boilerplate carries no holding
        if re.search(r"IN THE (SUPERIOR|SUPREME) COURT|A/K/A|No\. \d+ WDA|J-A\d{5}", t):
            sc -= 2
        scored.append((sc, t))
    scored.sort(key=lambda x: -x[0])
    out = " ".join(t for sc, t in scored if sc > 0)[:limit]
    return out or " ".join(t for _s, t in scored[:2])[:limit]


def _holding_sentence_llm(cite: str, name: str = "") -> str:
    """LLM SELECTS the holding from the authority's OWN corpus text; gate VALIDATES it.

    Returns "" when corpus has no text or nothing survives the form gate -- an empty
    holding is honest; an invented one is a fabricated citation.
    """
    txt = _authority_own_text(cite, name, limit=6000) or _holding_text(cite, name, limit=6000)
    if not txt:
        return ""
    try:
        from pipeline.claim_support_check import _complete as _llm
    except Exception:
        return ""
    auth = f"{name}, {cite}" if name else cite
    prompt = (_HOLDING_PROMPT.replace("{auth}", auth)
                             .replace("{grounding}", txt[:6000]))
    raw = _llm(prompt, max_tokens=180, timeout=90) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return ""
    import json as _json
    try:
        h = (_json.loads(m.group(0)).get("holding") or "").strip()
    except Exception:
        return ""
    if not h or h.upper().startswith("NONE"):
        return ""
    ok, _why = _holding_form_ok(h, cite, name)
    return h if ok else ""



# --- quoted-RULE verbatim gate ------------------------------------------------
# m0 quoted Pa.R.C.P. 1032(a) in the analysis and INVENTED part of the tail:
#   enacted: "...failure to exercise or exhaust a statutory remedy and an adequate
#             remedy at law and any other nonwaivable defense or objection."
#   m0 wrote: "...the defenses of failure to state a cause of action, and the defense
#             of failure to join an indispensable party."   <- fabricated + duplicated
# 0.805 similarity. A misquoted RULE inside quotation marks is a misrepresentation to
# the court, and it is the one defect class no downstream drafter can catch (m1 copies
# the quote faithfully). Gate it where the quote is WRITTEN: compare every quoted span
# adjacent to a rule cite against the enacted text in corpus, and mark any span that
# does not match so it cannot be filed as a quote.

_RULE_CITE_NEAR_QUOTE = re.compile(
    r"(Pa\.?\s?R\.?C\.?P\.?\s?(?:No\.\s?)?\d{3,4}(?:\([a-z0-9]+\))*)"
    r"[^\u201c\"]{0,40}[\u201c\"]([^\u201d\"]{25,1600})[\u201d\"]", re.I)


def _norm_q(s: str) -> str:
    s = re.sub(r"[\u201c\u201d\"]", "", s or "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\[\]]", "", s)
    return s.strip().lower().rstrip(".")


def _quote_phrase_in_corpus(quote: str, rule_num: str = "") -> tuple:
    """(found, source, window) for a DISTINCTIVE phrase of the quote, by CONTENT.

    Source-name family filters are unreliable -- Pa.R.C.P. 1028(b)'s enacted text sits in
    `pa_code_t231_ch3000` and in a rulemaking-proposal PDF, while Philadelphia TRAFFIC
    COURT Rule 1032 answers a `"Rule 1032."` probe. So do not guess the family: take a
    distinctive run of words FROM THE QUOTE and ask corpus whether those exact words
    exist. Words the quote invented exist nowhere, which is the signal we want.
    """
    import sqlite3 as _sq
    q = " ".join((quote or "").split())
    if len(q) < 25:
        return (False, "", "")
    # a distinctive interior run, away from boilerplate openings
    words = q.split()
    probes = []
    for start in (0, max(0, len(words) // 3), max(0, (2 * len(words)) // 3)):
        seg = " ".join(words[start:start + 9]).strip(" ,;:.\u201c\u201d\"")
        if len(seg) > 28:
            probes.append(seg)
    try:
        con = _corpus_connect()
        cur = con.cursor()
        for probe in probes:
            safe = probe.replace('"', "")
            cur.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT 8",
                        (f'"{safe}"',))
            for (sid,) in cur.fetchall():
                cur.execute("SELECT source, text FROM chunk_text WHERE chunk_id=?", (sid,))
                row = cur.fetchone()
                if not row or not row[1]:
                    continue
                t = " ".join(row[1].split())
                if rule_num and f"Rule {rule_num}" not in t and f"{rule_num}(" not in t:
                    continue
                i = _norm_q(t).find(_norm_q(safe))
                con.close()
                return (True, row[0], t[max(0, i - 200):i + 1400])
        con.close()
    except CorpusLocked:
        raise
    except Exception:
        pass
    return (False, "", "")


def _enacted_rule_text(rule: str, must_contain: str = "") -> str:
    """Enacted text for a Pa.R.C.P. cite -- CIVIL family only, content-verified.

    Rule numbers are NOT unique across Pennsylvania rule sets: a `"Rule 1032."` probe
    returned Philadelphia TRAFFIC COURT Rule 1032 ("Pleas in Response to Citation")
    for Pa.R.C.P. 1032 (Waiver of Defenses), and a Rule 1028 probe landed on the (c)
    subdivision while the quote under test was (b). Verdicts computed against the wrong
    rule are worse than no gate, so: restrict to civil-rules sources, and REQUIRE the
    retrieved window to contain a distinctive phrase from the quote before using it.
    Returns "" when the right text cannot be confirmed -- honest abstention.
    """
    num = re.search(r"(\d{3,4})", rule or "")
    if not num:
        return ""
    n = num.group(1)
    need = _norm_q(must_contain)[:60]
    try:
        import sqlite3 as _sq
        con = _corpus_connect()
        cur = con.cursor()
        cur.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT 60",
                    (f'"Rule {n}."',))
        ids = [r[0] for r in cur.fetchall()]
        cands = []
        for sid in ids:
            cur.execute("SELECT source, text FROM chunk_text WHERE chunk_id=?", (sid,))
            row = cur.fetchone()
            if not row or not row[1]:
                continue
            src, t = (row[0] or ""), " ".join(row[1].split())
            low = src.lower()
            # CIVIL family only -- exclude traffic/criminal/juvenile/orphans rule sets
            if any(b in low for b in ("traffic", "criminal", "juvenile", "orphan", "appellate")):
                continue
            if not any(g in low for g in ("civil", "trial", "cts", "rules", "prm")):
                continue
            for m in re.finditer(rf"Rule {n}\.", t):
                seg = t[m.start():m.start() + 2600]
                cands.append(seg)
        con.close()
        if not cands:
            return ""
        if need:
            # content check: the enacted window must actually carry the quoted language
            for seg in sorted(cands, key=len, reverse=True):
                if need[:40] in _norm_q(seg):
                    return seg
            # a distinctive fragment of the quote is absent from EVERY candidate ->
            # either the quote is fabricated or the rule text is not in corpus. Return
            # the longest civil candidate so the caller can report the divergence.
            return max(cands, key=len)
        return max(cands, key=len)
    except CorpusLocked:
        raise
    except Exception:
        return ""


def _elision_verdict(quote: str, enacted: str):
    """Flag a VERBATIM quote that stops inside the rule's EXCEPTION list.

    A quote can be word-for-word accurate and still misstate the rule. m0 elided
    Pa.R.C.P. 1032(a) at "...the defenses of failure to..." -- every quoted word real,
    but the surviving text lists only some of the exceptions and omits the catch-all
    ("any other nonwaivable defense or objection"). Read downstream, that is a waiver
    rule with a SHORT exception list, and the draft duly asserted waiver of grounds the
    rule expressly excepts. An elision inside an exception/proviso must be marked so the
    drafter quotes the operative clause whole or not at all.
    """
    qn = " ".join((quote or "").split())
    en = " ".join((enacted or "").split())
    if not qn or not en:
        return None
    # Did the author signal an elision at the END of the quote?
    if not re.search(r"(?:\.\s*\.\s*\.|\u2026)\s*[\"\u201d]?\s*$", qn):
        return None
    core = qn.rstrip(" .\u2026\"\u201d")
    i = en.find(core[-60:]) if len(core) > 60 else en.find(core)
    if i < 0:
        return None
    rest = en[i + len(core[-60:] if len(core) > 60 else core):]
    # Only exception/proviso language matters -- eliding a trailing example list is fine.
    if not re.search(r"\bexcept\b|\bunless\b|\bprovided\b|nonwaivable", qn, re.I):
        return None
    if re.search(r"nonwaivable|any other|\bexcept\b", rest[:400], re.I):
        return "ELIDED_EXCEPTION"
    return None


def _quoted_rules_audit(doc: str) -> list:
    """Every (rule, quote, verdict, similarity) for rule quotes in the analysis."""
    import difflib
    out = []
    for m in _RULE_CITE_NEAR_QUOTE.finditer(doc or ""):
        rule, quote = m.group(1), m.group(2)
        num = re.search(r"(\d{3,4})", rule)
        rn = num.group(1) if num else ""
        found, src, window = _quote_phrase_in_corpus(quote, rn)
        # TAIL TEST -- the decisive one. A fabricated quote copies the opening faithfully
        # and invents the close, so a prefix probe always succeeds. Ask corpus for the
        # quote's CLOSING language on its own.
        _w = " ".join((quote or "").split()).split()
        tail_ok = True
        if len(_w) >= 14:
            tail = " ".join(_w[-9:]).strip(" ,;:.\u201c\u201d\"")
            tail_ok = _quote_phrase_in_corpus(tail, rn)[0] if len(tail) > 28 else True
        if found and not tail_ok:
            out.append((rule, quote, "MISQUOTED_TAIL", 0.0))
            continue
        if found:
            q, e = _norm_q(quote), _norm_q(window)
            if q in e:
                _v = _elision_verdict(quote, e)
                out.append((rule, quote, _v or "VERBATIM", 1.0))
                continue
            # the quote's opening language is real; check whether the WHOLE span is
            sm0 = difflib.SequenceMatcher(None, q, e)
            _a, _b, _sz = sm0.find_longest_match(0, len(q), 0, len(e))
            cov = _sz / max(1, len(q))
            if cov >= 0.97:
                out.append((rule, quote, "VERBATIM", round(cov, 3)))
            else:
                out.append((rule, quote, "MISQUOTED", round(cov, 3)))
            continue
        enacted = _enacted_rule_text(rule, quote)
        if not enacted:
            out.append((rule, quote, "QUOTE_NOT_IN_CORPUS", 0.0))
            continue
        q, e = _norm_q(quote), _norm_q(enacted)
        if q in e:
            out.append((rule, quote, "VERBATIM", 1.0))
            continue
        # Not a substring. Anchor on difflib's longest matching block so the comparison
        # window is ALIGNED -- a blind fixed-step sweep straddles the true offset and
        # reports a falsely low ratio (0.374 on text that is ~80% identical).
        sm = difflib.SequenceMatcher(None, q, e)
        a, b, size = sm.find_longest_match(0, len(q), 0, len(e))
        if size < 20:
            out.append((rule, quote, "MISQUOTED", 0.0))
            continue
        start = max(0, b - a)
        win = e[start:start + len(q) + 40]
        best = difflib.SequenceMatcher(None, q, win).ratio()
        # Report the DIVERGENCE POINT: where the quote stops tracking the enacted text.
        out.append((rule, quote, "MISQUOTED" if best < 0.97 else "VERBATIM", round(best, 3)))
    return out


def _fold_quoted_rule_gate(slug: str, doc: str) -> str:
    """Mark misquoted rule text ON THE RECORD so no drafter can file it as a quote."""
    audit = _quoted_rules_audit(doc)
    bad = [a for a in audit if a[2] != "VERBATIM"]  # incl. MISQUOTED_TAIL
    if not audit:
        return doc
    print(f"  [rulequote] {len(audit)} quoted rule(s); {len(bad)} not verbatim")
    if not bad:
        return doc
    rows = ["", "### QUOTED RULE TEXT — VERBATIM CHECK", "",
            "_A rule quoted inside quotation marks must match the enacted text. Any entry "
            "below marked MISQUOTED must be replaced with the enacted text before filing; "
            "do not file the analysis's paraphrase as a quotation._", ""]
    for rule, quote, verdict, sim in bad:
        enacted = _enacted_rule_text(rule, quote)
        rows.append(f"- **{rule}** — {verdict} (similarity {sim}).")
        rows.append(f"    - analysis wrote: \u201c{quote[:300]}\u2026\u201d")
        if enacted:
            i = enacted.lower().find("(a)")
            rows.append(f"    - ENACTED [corpus]: \u201c{enacted[max(0,i):i+700]}\u2026\u201d")
        rows.append("")
    for rule, quote, verdict, sim in bad:
        # neutralize the bad quote IN PLACE so a drafter copying prose cannot ship it
        doc = doc.replace(f"\u201c{quote}\u201d",
                          f"[MISQUOTED — see QUOTED RULE TEXT block; use enacted text] \u201c{quote}\u201d", 1)
    return doc + "\n" + "\n".join(rows)



_BARE_CITE_RUN = re.compile(
    r"(?:the )?controlling authority (?:is|are)\s+(.{0,400}?)"
    r"(?=\s*(?:For\s+Count|For\s+the\s+Count|\n\n)|\Z)", re.I | re.S)
_CITE_IN_RUN = re.compile(r"(\d{2,3}\s+(?:A\.\d[a-z]?|U\.S\.|F\.Supp\.\d?[a-z]?|P\.S\.)\s*\d+)")


def _bare_cite_audit(doc: str) -> list:
    """(cite, run) for authorities cited with no holding beside them.

    *210(4) forbids citing authority "for general reference"; the holding must sit
    immediately before or after the cite. The analysis's "For Count II, the controlling
    authority is X, 729 A.2d 1200 (Pa. Super. 1999) and Y, 106 A.3d 48 (Pa. 2014)."
    names four authorities across two counts and states not one holding.
    """
    out = []
    for m in _BARE_CITE_RUN.finditer(doc or ""):
        run = " ".join(m.group(1).split())
        cites = _CITE_IN_RUN.findall(run)
        if not cites:
            continue
        # a run that states a holding carries a verb of decision; a bare run does not
        if re.search(r"\b(held|holds|holding|requires?|establishes?|permits?|provides?|"
                     r"must show|does not|bars?|precludes?|applies)\b", run, re.I):
            continue
        for c in cites:
            out.append((c.strip(), run[:220]))
    return out


def _fold_bare_cite_gate(slug: str, doc: str) -> str:
    """Attach the corpus holding to every authority the analysis cited bare."""
    bare = _bare_cite_audit(doc)
    if not bare:
        return doc
    seen, rows = set(), []
    for cite, run in bare:
        if cite in seen:
            continue
        seen.add(cite)
        nm = ""
        for c2, n2 in list(_our_pleaded_authorities(slug).items()) + \
                      list(_their_authorities(slug).items()):
            if c2.replace(" ", "") == cite.replace(" ", ""):
                nm = n2
                break
        rule = _corpus_rule_sentence(cite, nm)
        if rule:
            ok, why = _holding_form_ok(rule, cite, nm)
            if not ok:
                rule = ""
        if not rule:
            rule = _holding_sentence_llm(cite, nm)
        rows.append((cite, nm, rule, run))
    print(f"  [barecite] {len(rows)} authority(ies) cited without a holding (*210(4))")
    blk = ["", "### BARE CITES — HOLDING REQUIRED BEFORE FILING", "",
           "_Phila.Civ.R. *210(4): authority \u201cshall not be cited for general reference but "
           "shall be immediately preceded or followed by its relevant holding.\u201d Each cite "
           "below appears in the analysis with no holding. The holding supplied here is from "
           "corpus and must travel with the cite into the brief._", ""]
    for cite, nm, rule, run in rows:
        blk.append(f"- **{nm or ''} {cite}** — "
                   + (rule if rule else "[holding not in corpus — PULL BEFORE FILING]"))
        blk.append(f"    - cited bare as: \u201c\u2026{run}\u2026\u201d")
        blk.append("")
    return (doc or "") + "\n" + "\n".join(blk)


_BINDING_RULE_PATTERNS = (
    "not bound by the decisions of federal courts",
    "not bound by the federal district court",
    "absence of a United States Supreme Court pronouncement",
    "decisions of the lower federal courts are not binding",
    "federal court decisions are not binding",
    "not binding on this Court",
    "are not binding authority",
    "persuasive but not binding",
    "decisions of other states are not binding",
    "not bound by decisions of",
)


def _binding_weight_rule(prefer_cites: tuple = ()) -> tuple:
    """Return (quote, source_cite) for the rule that non-binding authority does not bind.

    Preference order: an authority the OPPONENT cited (their own case defeats them), then
    any authority in corpus. Returns ("", "") when corpus has nothing -- the caller then
    states the binding weight without a quote rather than inventing one.
    """
    import sqlite3 as _sq
    try:
        con = _corpus_connect()
    except CorpusLocked:
        raise
    except Exception:
        return ("", "")
    cur = con.cursor()

    def _hunt(where_cite: str = ""):
        for pat in _BINDING_RULE_PATTERNS:
            if where_cite:
                cur.execute(
                    "SELECT citations, text FROM chunk_text "
                    "WHERE citations LIKE ? AND text LIKE ? LIMIT 1",
                    ("%" + where_cite + "%", "%" + pat + "%"))
            else:
                cur.execute(
                    "SELECT citations, text FROM chunk_text WHERE text LIKE ? LIMIT 1",
                    ("%" + pat + "%",))
            row = cur.fetchone()
            if not row:
                continue
            cites, txt = row[0] or "", " ".join((row[1] or "").split())
            i = txt.lower().find(pat.lower())
            if i < 0:
                continue
            # widen to sentence boundaries around the hit
            start = txt.rfind(". ", 0, max(0, i - 40))
            start = 0 if start < 0 else start + 2
            end = txt.find(". ", i + len(pat))
            end = len(txt) if end < 0 else end + 1
            quote = txt[start:end].strip()
            if 60 <= len(quote) <= 600:
                return (quote, (where_cite or (cites.split(";")[0].strip() if cites else "")))
        return ("", "")

    for c in prefer_cites:                      # their own authority first
        q, src = _hunt(c)
        if q:
            con.close()
            return (q, src or c)
    q, src = _hunt()                            # then anything in corpus
    con.close()
    return (q, src)


def _fold_nonbinding_authority(slug: str, block: str, doc: str = "") -> str:
    """State, with proof, that the opponent's non-binding authority does not bind.

    Reads m0's OWN ledger for the binding determination -- this organ adds the rule and
    its source, never a holding of its own.
    """
    if "NON-BINDING AUTHORITY" in (block or ""):
        return block
    import re as _re
    _NB_MARK = ("persuasive only", "persuasive at most", "not binding", "does not bind",
                "no (persuasive", "| no |", "not bound")
    _CITE = _re.compile(r"(\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.|U\.S\.|"
                        r"F\.\s?Supp\.?\s?\d?[a-z]{0,2}|F\.\d[a-z]{0,2})\s+\d+)")
    theirs = _their_authorities(slug) or {}
    nonbinding, _seen = [], set()
    for line in (block or "").splitlines():
        low = line.lower()
        if not any(k in low for k in _NB_MARK):
            continue
        cm = _CITE.search(line)
        if not cm:
            continue
        cite = cm.group(1).strip()
        if cite in _seen:
            continue
        # only the OPPONENT's authorities: ours being persuasive is not a point for us
        if theirs and cite not in theirs:
            continue
        _seen.add(cite)
        # name = the italic/bold run before the cite, else the text before it
        head = line[:cm.start()]
        nm = _re.sub(r"^[-*\s|]+", "", head).strip(" *_,|")
        nm = _re.sub(r"\s{2,}", " ", nm)
        nonbinding.append((nm, cite, "", ""))
    if not nonbinding:
        return block
    quote, src = _binding_weight_rule(tuple(theirs.keys()))
    out = ["", "### NON-BINDING AUTHORITY THE MOVANT RELIES ON", ""]
    names = "; ".join((f"{n}, {c}" if n else c) for n, c, _ct, _cf in nonbinding)
    out.append(f"- The movant's objections rest in part on {names}. Each is a decision of "
               f"a court that does not bind this Court.")
    if quote:
        whose = " — an authority the movant itself cites" if src in theirs else ""
        out.append(f"- The rule is stated in {src}{whose}: \"{quote}\"")
        out.append(f"- The movant has identified no pronouncement of the Supreme Court of "
                   f"the United States, and no decision of the Supreme Court of "
                   f"Pennsylvania or the Superior Court, adopting the propositions it "
                   f"draws from those decisions. They are therefore persuasive at most, "
                   f"and the movant's failure to supply binding authority for a "
                   f"categorical proposition of Pennsylvania law is fatal to the "
                   f"objection that rests on it.")
    else:
        out.append(f"- The movant has identified no binding authority of this "
                   f"Commonwealth adopting the propositions it draws from those "
                   f"decisions.")
    out.append("")
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + out + lines[1:])
    return "\n".join(out) + "\n" + body


_DISTINGUISH_SYS = (
    "You are a Pennsylvania appellate practitioner writing one paragraph of an opposition "
    "brief. You will be given ONE authority the opposing party relies on, the holding "
    "text of that authority from the official reporter, and the established facts of "
    "THIS record. Write the factual distinction: what the cited case turned on, what this "
    "record shows instead, and why the rule therefore does not reach the claim as "
    "pleaded. Requirements: cite only the authority given and the record facts given; "
    "quote the holding language when it helps; do not characterize the opinion beyond "
    "the text supplied; no headings, no bullet points, no preamble. If the record does "
    "not supply a genuine factual difference, reply with exactly: NO DISTINCTION "
    "AVAILABLE -- and nothing else. Never invent a fact, a date, or a holding."
)


def _holding_text(cite: str, name: str = "", limit: int = 2600) -> str:
    """Holding language for an authority, from corpus only."""
    import sqlite3 as _sq
    try:
        con = _corpus_connect()
    except CorpusLocked:
        raise
    except Exception:
        return ""
    cur = con.cursor()
    rows = []
    for q, a in (("SELECT text FROM chunk_text WHERE citations = ? LIMIT 6", (cite,)),
                 ("SELECT text FROM chunk_text WHERE citations LIKE ? LIMIT 6",
                  ("%" + cite + "%",)),
                 ("SELECT text FROM chunk_text WHERE text LIKE ? LIMIT 4",
                  ("%" + cite + "%",))):
        try:
            cur.execute(q, a)
            rows = [r[0] for r in cur.fetchall() if r and r[0]]
        except Exception:
            rows = []
        if rows:
            break
    con.close()
    if not rows:
        return ""
    # prefer chunks that read like holdings
    import re as _re
    scored = []
    for t in rows:
        t = " ".join(t.split())
        sc = sum(t.lower().count(k) for k in
                 ("we hold", "held that", "the rule", "must establish", "elements of",
                  "we conclude", "affirmed", "reversed", "gravamen", "turns on"))
        scored.append((sc, t))
    scored.sort(key=lambda x: -x[0])
    return " ".join(t for _sc, t in scored[:2])[:limit]


def _record_facts_text(slug: str, block: str, limit: int = 2200) -> str:
    """Our established record facts, as m0 already folded them."""
    import re as _re
    m = _re.search(r"^#*\s*RECORD FACTS[^\n]*\n", block or "", _re.M)
    if m:
        nxt = _re.search(r"\n#{2,6}\s", block[m.end():])
        return " ".join(block[m.end(): m.end() + (nxt.start() if nxt else len(block))]
                        .split())[:limit]
    # fall back to the intake record facts m0 maintains
    try:
        p = CASES / slug / "CASE_FACTS.md"
        doc = p.read_text(encoding="utf-8")
        m2 = _re.search(r"^#*\s*RECORD FACTS[^\n]*\n", doc, _re.M)
        if m2:
            nxt = _re.search(r"\n#{2,6}\s", doc[m2.end():])
            return " ".join(doc[m2.end(): m2.end() + (nxt.start() if nxt else len(doc))]
                            .split())[:limit]
    except Exception:
        pass
    return ""



# ══════════════════════════════════════════════════════════════════════════════
# ADVERSE-AUTHORITY PROVENANCE (DETECT, 2026-09-27). For every authority the MOVANT
# cites, read the WHOLE opinion from corpus -- head (caption, counsel of record) and tail
# (disposition) -- not the top-k "holding-like" chunks, which is why this was never seen:
# Rancosky, 170 A.3d 364 (Pa. 2017) was argued for the insurer by the movant's own firm
# (Gordon & Rees) and the insurer LOST; Kelly, 159 F.Supp.3d 562 (E.D. Pa. 2016) was the
# movant's own case, argued by the same signing attorney, and the movant LOST the
# bad-faith half. Both were cited to this Court as settled law for the movant. The
# deterministic layer finds name/firm/party overlap (guardrail); the LLM reads the
# counsel section + disposition and says who represented whom and who prevailed
# (comprehension). Agnostic: firm, attorneys and party names come from the movant's
# own signature block and caption, never from a table.
# ══════════════════════════════════════════════════════════════════════════════

_PROV_SYS = (
    "You are reading ONE judicial opinion for provenance only. You are given: the CAPTION "
    "and COUNSEL-OF-RECORD section, the SIDE the citing party (or its firm / attorney) "
    "appeared on in that case -- already determined from the counsel listing -- the "
    "DISPOSITION text (the part of the opinion that decides it), and the PROPOSITION the "
    "citing party now attributes to this opinion. Decide from the DISPOSITION text whether "
    "THAT SIDE obtained the result it sought. Read the disposition carefully: 'affirm the "
    "vacatur of a judgment for X' is a LOSS for X; 'reverse' a ruling against Y is a WIN for "
    "Y; a motion 'granted in part and denied in part' is PARTIAL for the movant. Answer in "
    'STRICT JSON, no prose: {"that_side_prevailed":"YES|NO|PARTIAL|UNCLEAR",'
    '"disposition_quote":"<the sentence from the DISPOSITION text that decides it>",'
    '"proposition_held":"YES|NO|PARTIAL|UNCLEAR","proposition_quote":"<sentence from the '
    'text that supports or contradicts the proposition, or NONE>"}. Quote only sentences '
    "present in the text supplied. Never infer a result the text does not state."
)

_ROLE_RX = None


def _side_from_counsel(head: str, needles: list) -> str:
    """Deterministic: the role ("Appellant", "Appellee", "Plaintiff", ...) that FOLLOWS the
    first counsel-listing occurrence of any needle (firm word / attorney surname / party
    word) -- reporters list counsel as "A, B, Firm, for Appellant." -- resolved to the party
    named with that role in the caption when the caption states it ("X, Appellee v. Y,
    Appellant"). Returns "" if no needle sits inside a counsel listing."""
    import re as _re
    global _ROLE_RX
    if _ROLE_RX is None:
        _ROLE_RX = _re.compile(r"(?i)\bfor (?:the )?((?:cross-)?(?:appellants?|appellees?|"
                               r"plaintiffs?|defendants?|petitioners?|respondents?|movants?|"
                               r"intervenors?)(?:[ /-]*(?:appellants?|appellees?))?)"
                               r"(?:\s+amic\w+\s+curiae)?")
    hl = head.lower()
    best = None
    # district courts list "..., Firm, City, ST, for <Party Name>." -- no role word
    _named = _re.compile(r"\bfor ([A-Z][A-Za-z0-9&.,'\- ]{3,90}?)(?:\.\s|\.$|\n)")
    for nd in needles:
        for m_ in _re.finditer(r"\b" + _re.escape(nd.lower()) + r"\b", hl):
            r = _ROLE_RX.search(head, m_.end())
            r2 = _named.search(head, m_.end())
            for rr, kind in ((r, "role"), (r2, "named")):
                if rr and rr.start() - m_.end() < 700 and "amic" not in rr.group(0).lower():
                    cand = (rr.start() - m_.end(), kind, rr.group(1))
                    if best is None or cand[0] < best[0]:
                        best = cand
    if not best:
        return ""
    if best[1] == "named":
        return " ".join(best[2].split()).strip(" ,.")
    role = " ".join(best[2].split()).title()
    # caption "<A>, Appellee v. <B>, Appellant": the party for the role is the segment
    # between the nearest preceding "v." (or start) and ", <Role>", cut at its first comma
    cap = head[:1500]
    rm = _re.search(r",?\s+" + _re.escape(role.split()[0]) + r"\b", cap)
    party = ""
    if rm:
        seg = cap[:rm.start()]
        v = max(seg.rfind(" v. "), seg.rfind(" V. "), seg.rfind("\nv. "))
        seg = seg[v + 4:] if v >= 0 else seg
        seg = seg.split(",")[0]
        party = " ".join(seg.split()).strip(" ,")
        if len(party) > 90 or len(party) < 3:
            party = ""
    return f"{role} ({party})" if party else role


def _movant_identity(slug: str) -> dict:
    """Firm, attorney surnames and party names from the movant's OWN filing (signature
    block + caption). Deterministic; nothing case-specific hardcoded."""
    import re as _re
    txt = _adverse_packet(slug) or _adverse_text(slug) or ""
    if not txt:
        return {}
    firms, attys, parties = set(), set(), set()
    # firm: "its attorneys, <Firm> LLP" / a capitalised run ending in LLP|LLC|P.C.|PC
    for m in _re.finditer(r"([A-Z][A-Za-z&'.,\- ]{3,80}?\s(?:LLP|L\.L\.P\.|LLC|P\.C\.|PC|PLLC))\b", txt):
        f = " ".join(m.group(1).replace(",", " ").split())
        f = _re.sub(r"^(?:attorneys?|counsel)\s+", "", f, flags=_re.I)
        if 2 <= len(f.split()) <= 8:
            firms.add(f)
    # attorneys: "/s/ First M. Last" and "First M. Last (PA I.D. 12345)"
    for m in _re.finditer(r"(?:/s/\s*|^\s*)([A-Z][a-z]+(?:\s[A-Z]\.)?(?:\s[A-Z][a-z]+){1,2})\s*(?:\(|,\s*Esq|$)", txt, _re.M):
        nm = m.group(1).strip()
        if _re.search(r"\((?:PA|NJ|NY)?\s*(?:I\.?D\.?|Bar|Atty)", txt[m.end()-1:m.end()+40]) or "/s/" in txt[max(0, m.start()-5):m.start()+1]:
            attys.add(nm)
    # party: "<Defendant> ... by and through its attorneys" and the caption's defendant
    for m in _re.finditer(r"(?:Defendant|Movant|Petitioner|Appellant)[,\s]+([A-Z][A-Za-z&.,'\- ]{3,90}?)(?:\s*\(|,?\s+by and through)", txt):
        p = " ".join(m.group(1).replace(",", " ").split())
        if 1 <= len(p.split()) <= 8:
            parties.add(p)
    # OCR/typo variants of one firm ("MANSUKAHNI LLP" vs "Defense Firm
    # keep the longest name whose significant tokens cover the shorter one
    def _toks(f):
        return {w.lower().strip(".,") for w in f.split() if len(w) > 3 and w.upper() not in ("LLP", "LLC", "PLLC")}
    def _sim(a, b):
        return sum(1 for x in a for y in b if x == y or (len(x) > 5 and len(y) > 5 and sum(c1 != c2 for c1, c2 in zip(x, y)) + abs(len(x) - len(y)) <= 2))
    keep = []
    for f in sorted(firms, key=len, reverse=True):
        if not any(_sim(_toks(f), _toks(k)) >= max(1, len(_toks(f))) for k in keep):
            keep.append(f)
    return {"firms": keep, "attorneys": sorted(attys), "parties": sorted(parties)}


def _full_opinion(cite: str, name: str = "") -> tuple:
    """(source, head_text, tail_text, full_len) for an authority -- ALL chunks in order.
    Head = caption + counsel of record; tail = disposition. Empty tuple if not in corpus."""
    try:
        con = _corpus_connect()
    except CorpusLocked:
        raise
    except Exception:
        return ()
    cur = con.cursor()
    src = ""
    for q, a in (("SELECT source FROM chunk_text WHERE citations = ? LIMIT 1", (cite,)),
                 ("SELECT source FROM chunk_text WHERE citations LIKE ? LIMIT 1", ("%" + cite + "%",)),
                 ("SELECT source FROM chunk_text WHERE text LIKE ? LIMIT 1", ("%" + cite + "%",))):
        try:
            cur.execute(q, a)
            r = cur.fetchone()
        except Exception:
            r = None
        if r and r[0]:
            src = r[0]
            break
    if not src:
        con.close()
        return ()
    cur.execute("SELECT text FROM chunk_text WHERE source = ? ORDER BY chunk_id", (src,))
    parts = [r[0] for r in cur.fetchall() if r and r[0]]
    con.close()
    full = "\n".join(parts)
    # the ingester sometimes stores the document twice back-to-back; keep one copy
    half = len(full) // 2
    if half > 2000 and full[:1500] == full[half:half + 1500]:
        full = full[:half]
    return (src, full[:6000], _disposition_window(full), len(full))


_DISP_RX = None


def _disposition_window(full: str, width: int = 5000) -> str:
    """The part of an opinion that DECIDES it. chunk_id order is not document order in
    corpus (Rancosky's last chunk was procedural history), so the tail cannot be assumed
    to be the disposition: find the densest cluster of dispositive language instead, and
    fall back to the tail only when none exists."""
    import re as _re
    global _DISP_RX
    if _DISP_RX is None:
        _DISP_RX = _re.compile(
            r"(?i)\b(we (?:hold|conclude|therefore hold|reverse|affirm|vacate|remand)|"
            r"(?:is|are) (?:hereby )?(?:affirmed|reversed|vacated|remanded|granted|denied|"
            r"dismissed|sustained|overruled)|(?:order|judgment) (?:of the [a-z ]+ court )?"
            r"(?:is|be) (?:affirmed|reversed|vacated)|granted in part|denied in part|"
            r"jurisdiction (?:is )?relinquished|for the foregoing reasons|accordingly, we)\b")
    if not full:
        return ""
    pos = [m_.start() for m_ in _DISP_RX.finditer(full)]
    if not pos:
        return full[-width:]
    # densest window: most hits within `width` chars, prefer the latest such cluster
    best_i, best_n = 0, -1
    for i, p in enumerate(pos):
        n = sum(1 for q in pos if p <= q < p + width)
        if n >= best_n:
            best_i, best_n = i, n
    start = max(0, pos[best_i] - width // 4)
    return full[start:start + width]


def _prevailed_from_posture(side: str, disposition: str) -> str:
    """Deterministic guardrail on appellate/motion posture. An AFFIRMANCE is a loss for the
    appellant and a win for the appellee; REVERSAL/VACATUR the converse; a motion "granted
    in part and denied in part" is PARTIAL for the movant. Returns "" when the text is
    mixed or the side is unknown -- the model then decides."""
    import re as _re
    d = " ".join((disposition or "").lower().split())
    if not d:
        return ""
    aff = bool(_re.search(r"\b(?:is|are|be)\s+affirmed\b|\bwe affirm\b", d))
    rev = bool(_re.search(r"\b(?:is|are|be)\s+(?:reversed|vacated)\b|\bwe (?:reverse|vacate)\b", d))
    part = bool(_re.search(r"granted in part|denied in part|in part and", d))
    gr = bool(_re.search(r"\b(?:is|are|be|shall be)\s+granted\b|\bwe (?:shall )?grant\b", d))
    dn = bool(_re.search(r"\b(?:is|are|be|shall be)\s+denied\b|\bwe (?:shall )?deny\b", d))
    role = (side or "").lower()
    if role.startswith("appellant") or role.startswith("petitioner"):
        # "affirm the ... disposition to vacate the trial court's judgment" = affirmance
        if aff and not (rev and not _re.search(r"affirm[^.]{0,80}(?:vacat|revers)", d)):
            return "NO"
        if rev and not aff:
            return "YES"
        return ""
    if role.startswith("appellee") or role.startswith("respondent"):
        if aff and not (rev and not _re.search(r"affirm[^.]{0,80}(?:vacat|revers)", d)):
            return "YES"
        if rev and not aff:
            return "NO"
        return ""
    # trial-court posture: the movant is the side we matched only when it moved;
    # "granted in part and denied in part" is PARTIAL regardless of which side
    if part:
        return "PARTIAL"
    return ""


def _fold_adverse_authority_provenance(slug: str, block: str, doc: str = "") -> str:
    """Who argued the movant's authorities, for whom, and who won -- from the opinions."""
    block = _strip_fold_section(block, "PROVENANCE OF THE MOVANT'S AUTHORITIES")
    import re as _re, json as _json
    theirs = _their_authorities(slug) or {}
    if not theirs:
        return block
    ident = _movant_identity(slug)
    if not any(ident.get(k) for k in ("firms", "attorneys", "parties")):
        return block
    adverse_txt = _adverse_text(slug) or ""
    # match tokens: firm words (drop LLP etc.), attorney surnames, party words
    _stop = {"llp", "llc", "pc", "p.c.", "the", "and", "of", "company", "insurance", "inc", "co",
             "group", "advanced", "national", "general", "american", "united", "first", "mutual",
             "casualty", "assurance", "services", "financial", "corporation", "bank", "trust",
             "holdings", "international", "associates", "partners", "county", "state", "city"}
    firm_toks = {w.lower().strip(".,") for f in ident.get("firms", []) for w in f.split()
                 if w.lower().strip(".,") not in _stop and len(w) > 3}
    atty_surnames = {a.split()[-1].lower() for a in ident.get("attorneys", [])}
    party_toks = {w.lower().strip(".,") for p in ident.get("parties", []) for w in p.split()
                  if w.lower().strip(".,") not in _stop and len(w) > 3}
    hits, checked = [], 0
    for cite, name in theirs.items():
        op = _full_opinion(cite, name)
        if not op:
            continue
        src, head, tail, n = op
        checked += 1
        hl = head.lower()
        f_hit = sorted(t for t in firm_toks if _re.search(r"\b" + _re.escape(t) + r"\b", hl))
        a_hit = sorted(t for t in atty_surnames if _re.search(r"\b" + _re.escape(t) + r"\b", hl))
        p_hit = sorted(t for t in party_toks if _re.search(r"\b" + _re.escape(t) + r"\b", hl))
        # a firm needs >=2 distinctive words, or 1 word plus an attorney, to count
        firm_match = len(f_hit) >= 2 or (len(f_hit) == 1 and a_hit)
        # a party matches only on a DISTINCTIVE token ("progressive"), never on generic
        # corporate vocabulary ("advanced" matched Vives v. Rodriguez on 2026-09-27)
        party_match = any(len(t) > 6 and t not in _stop for t in p_hit)
        if not (firm_match or a_hit or party_match):
            continue
        # the movant's stated proposition: the sentence in which the cite sits
        prop = ""
        pos = adverse_txt.find(cite)
        if pos >= 0:
            s0 = max(adverse_txt.rfind(". ", 0, pos), adverse_txt.rfind("\n\n", 0, pos), 0)
            s1 = adverse_txt.find(". ", pos)
            prop = " ".join(adverse_txt[s0: (s1 + 1) if s1 > 0 else pos + 300].split())[:600]
        # counsel section = lines around the first counsel keyword in the head
        cm = _re.search(r"(?i)(for (?:the )?(?:appellant|appellee|plaintiff|defendant|petitioner|respondent)|counsel of record|attorneys? for)", head)
        counsel = head[max(0, (cm.start() if cm else 0) - 900):(cm.start() if cm else 0) + 1500]
        # GUARDRAIL: which side the match sits on is read off the counsel listing, not
        # asked of the model (it answered NONE for "Defense Firm
        side_det = _side_from_counsel(head, list(f_hit) + list(a_hit) + list(p_hit))
        prompt = (f"CAPTION AND COUNSEL-OF-RECORD SECTION:\n{counsel}\n\n"
                  f"THE CITING PARTY / ITS FIRM / ITS ATTORNEY APPEARED IN THIS CASE FOR: "
                  f"{side_det or 'UNCLEAR from the listing'}\n\n"
                  f"DISPOSITION TEXT:\n{tail[-3200:]}\n\n"
                  f"PROPOSITION THE CITING PARTY NOW ATTRIBUTES TO THIS OPINION:\n{prop or '(none stated)'}\n\n"
                  f"CITING PARTY: {'; '.join(ident.get('parties') or ['(unknown)'])}\n"
                  f"ITS FIRM(S): {'; '.join(ident.get('firms') or ['(unknown)'])}\n"
                  f"ITS ATTORNEYS: {'; '.join(ident.get('attorneys') or ['(unknown)'])}")
        ans = {}
        try:
            raw = (_call_direct(_PROV_SYS + "\n\n" + prompt, max_tokens=500, timeout=240) or "").strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            ans = _json.loads(raw) if raw else {}
        except CorpusLocked:
            raise
        except Exception:
            ans = {}
        # GUARDRAIL: quotes must be in the text we gave it
        def _norm(x):
            return _re.sub(r"[\u2018\u2019\u201c\u201d\u00ad\-]", "", " ".join(str(x or "").split()).lower())

        def _ok(q):
            # quote must be IN the text we supplied (curly quotes / soft hyphens normalised)
            qn = _norm(q)
            return bool(qn) and qn != "none" and (qn[:60] in _norm(tail) or qn[:60] in _norm(head))
        dq = ans.get("disposition_quote") if _ok(ans.get("disposition_quote")) else ""
        # a sentence with the operative verb ("we affirm", "is reversed", "granted in part")
        # beats the model's pick whenever one exists: the model returned reasoning
        # sentences ("However, because it is unclear...") that pass the in-text guardrail
        if dq and not _re.search(r"(?i)\bwe (?:affirm|reverse|vacate|grant|deny|shall grant|shall deny)\b|"
                                 r"\b(?:is|are|be) (?:hereby )?(?:affirmed|reversed|vacated|granted|denied)\b|"
                                 r"granted in part|denied in part", dq):
            dq = ""
        if not dq and _DISP_RX:
            # deterministic fallback: the LAST complete sentence in the window that carries
            # a dispositive verb (the operative order comes after the reasoning)
            _strong = _re.compile(r"(?i)\bwe (?:affirm|reverse|vacate|grant|deny|shall grant|shall deny)\b|"
                                  r"\b(?:is|are|be) (?:hereby )?(?:affirmed|reversed|vacated|granted|denied)\b|"
                                  r"granted in part|denied in part")
            for _rx in (_strong, _DISP_RX):
                for _sm in reversed(list(_rx.finditer(tail))):
                    _s0 = tail.rfind(". ", 0, _sm.start()) + 2
                    _s1 = tail.find(". ", _sm.end())
                    if _s1 > 0 and 20 < _s1 - _s0 < 700:
                        dq = " ".join(tail[_s0:_s1 + 1].split())
                        dq = _re.sub(r"\s+[IVX]+\.\s+\w*$", "", dq)  # strip a trailing heading stub
                        break
                if dq:
                    break
        pq = ans.get("proposition_quote") if _ok(ans.get("proposition_quote")) else ""
        prevailed = _prevailed_from_posture(side_det, dq or tail) or \
            str(ans.get("that_side_prevailed") or "UNCLEAR").upper()
        hits.append({"cite": cite, "name": name or "", "source": src, "firm": f_hit, "atty": a_hit,
                     "party": p_hit, "side": side_det,
                     "prevailed": prevailed,
                     "disp_q": dq, "held": str(ans.get("proposition_held") or "").upper(),
                     "prop_q": pq, "prop": prop})
    if not hits:
        return block
    out = ["", "### PROVENANCE OF THE MOVANT'S AUTHORITIES", "",
           f"- Of the {checked} authorities the movant cites that are in corpus, {len(hits)} "
           f"involve the movant, its counsel, or its firm as a participant."]
    for h in hits:
        who = []
        if h["firm"]:
            who.append(f"the movant's firm ({', '.join(ident.get('firms') or [])})")
        if h["atty"]:
            who.append(f"the movant's signing counsel ({', '.join(a for a in ident.get('attorneys', []) if a.split()[-1].lower() in h['atty'])})")
        if h["party"]:
            who.append("the movant itself as a party")
        line = f"- **{h['name'] + ', ' if h['name'] else ''}{h['cite']}** — {'; '.join(who)}"
        if h["side"] and h["side"].upper() != "NONE":
            line += f" appeared for {h['side']}"
        if h["prevailed"] in ("NO", "PARTIAL"):
            line += (" and **that side did not prevail**" if h["prevailed"] == "NO"
                     else " and **that side prevailed only in part**")
        if h["disp_q"]:
            line += f': "{h["disp_q"]}"'
        out.append(line + f". <!-- corpus: {h['source']} -->")
        if h["held"] == "NO" and h["prop"]:
            out.append(f"  - The movant cites it for: \"{h['prop'][:300]}\" — the opinion does not so hold"
                       + (f'; it says: "{h["prop_q"]}"' if h["prop_q"] else "") + ".")
        elif h["held"] == "PARTIAL" and h["prop_q"]:
            out.append(f"  - The opinion's actual statement is narrower: \"{h['prop_q']}\".")
    out.append("- A party that argued and lost the very standard it now presents to this Court as "
               "settled owes the Court candor about that history (Pa.R.P.C. 3.3(a)(2): duty to "
               "disclose directly adverse controlling authority). The omission is itself a defect "
               "in the filing.")
    out.append("")
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + out + lines[1:])
    return "\n".join(out) + "\n" + body


_OMIT_SYS = (
    "A party (the MOVANT) cites an EARLIER case for a stated PROPOSITION. You are given a LATER "
    "opinion of a court whose decisions bind the forum, which discusses that earlier case. Decide what "
    "the later court held ON THE MOVANT'S PROPOSITION -- not merely whether it mentioned the earlier case. "
    "Answer ONLY as JSON: "
    '{"relation": "REJECTED|LIMITED|DISTINGUISHED|APPROVED|NONE", '
    '"quote": "<one verbatim sentence from the LATER opinion that best states its holding on the proposition; empty if NONE>", '
    '"what_it_holds": "<two sentences, plain: (1) what the later court held on the proposition; (2) whether the same '
    'argument the movant now makes was made to the later court and how it fared>", '
    '"our_heading": "<one sentence, max 22 words, in the voice of the party OPPOSING the movant, usable as an argument '
    'heading: begin with the later case SHORT NAME exactly as given, state what it did to the proposition '
    '(rejected / limited / held the opposite), and that the movant did not cite it>"}. '
    "REJECTED = the later court considered the same proposition (or the same argument, made by a similarly situated "
    "party) and ruled against it, or held the opposite. LIMITED = the later court confined the earlier case to its "
    "facts so the proposition no longer follows. DISTINGUISHED = the later court set the earlier case aside as "
    "inapplicable on facts like the present ones. APPROVED = the later court applied the earlier case for the "
    "movant's proposition. NONE = the later opinion says nothing about the proposition. "
    "The quote MUST be copied verbatim from the passages given."
)

_REPORTER_FROM_SRC = {"a2d": "A.2d", "a3d": "A.3d", "f2d": "F.2d", "f3d": "F.3d", "f4th": "F.4th",
                      "fsupp2d": "F.Supp.2d", "fsupp3d": "F.Supp.3d", "fsupp": "F.Supp.", "us": "U.S.",
                      "pa": "Pa.", "pasuper": "Pa. Super.", "sct": "S.Ct."}


def _opinion_headmatter(full: str) -> str:
    """The caption/court/counsel block of an opinion, wherever the ingester put it.
    Some sources store the body first and the headmatter last (docvault_pull_a2d_787_376)."""
    import re as _re
    f = full or ""
    m = _re.search(r"##\s*Headmatter", f)
    if m:
        return f[m.start(): m.start() + 3000]
    m = _re.search(r"(?i)(Supreme Court of Pennsylvania|Superior Court of Pennsylvania|Commonwealth Court of "
                   r"Pennsylvania|Supreme Court of the United States|United States Court of Appeals|"
                   r"United States District Court)", f)
    if m:
        return f[max(0, m.start() - 600): m.start() + 2400]
    return f[:3000]


def _cite_from_source_id(src: str) -> str:
    """'docvault_pull_a2d_787_376' -> '787 A.2d 376'; '' if the id carries no reporter."""
    import re as _re
    m = _re.search(r"_(a2d|a3d|f2d|f3d|f4th|fsupp2d|fsupp3d|fsupp|us|pa|pasuper|sct)_(\d+)_(\d+)$", src or "")
    if m:
        return f"{m.group(2)} {_REPORTER_FROM_SRC[m.group(1)]} {m.group(3)}"
    m = _re.search(r"(\d+)(a2d|a3d|f2d|f3d|f4th|fsupp2d|fsupp3d|us|pa)(\d+)$", src or "")
    if m:
        return f"{m.group(1)} {_REPORTER_FROM_SRC[m.group(2)]} {m.group(3)}"
    return ""


def _opinion_court(head: str) -> str:
    """Court that issued an opinion, read off its own header. '' if unclear."""
    import re as _re
    h = " ".join((head or "")[:3000].split())
    if _re.search(r"(?i)Supreme Court of Pennsylvania|\b\d+ Pa\. \d+\b", h):
        return "PA_SUPREME"
    if _re.search(r"(?i)Superior Court of Pennsylvania|Pa\. ?Super\.", h):
        return "PA_SUPERIOR"
    if _re.search(r"(?i)Commonwealth Court of Pennsylvania|Pa\. ?Cmwlth\.", h):
        return "PA_COMMONWEALTH"
    if _re.search(r"(?i)Supreme Court of the United States|\bU\.S\. \d+\b", h):
        return "US_SUPREME"
    if _re.search(r"(?i)Court of Appeals for the Third Circuit|3d Cir\.", h):
        return "CA3"
    if _re.search(r"(?i)District Court for the .* District of Pennsylvania|[EMW]\.D\. ?Pa\.", h):
        return "PA_DISTRICT"
    return ""

# for a state trial court (CCP): what binds. Federal district/CA3 are persuasive only on state law.
_BINDS_PA_CCP = {"PA_SUPREME", "US_SUPREME", "PA_SUPERIOR", "PA_COMMONWEALTH"}
_BINDS_EDPA = {"US_SUPREME", "CA3", "PA_SUPREME"}  # PA Supreme binds on PA law questions

def _strip_fold_section(block: str, heading: str) -> str:
    """Remove the '### <heading>' subsection (through the next '### ' or end) so a fold can rebuild it.
    A fold that returns early when its heading is already present can never update its own output --
    the fourth 'silent stale' of 2026-09-27. Folds must REPLACE, never skip."""
    import re as _re
    if not block or heading not in block:
        return block or ""
    rx = _re.compile(r"(?ms)^### " + _re.escape(heading) + r"[^\n]*\n.*?(?=^### |\Z)")
    return rx.sub("", block).rstrip("\n") + "\n"


def _clean_prop(prop: str) -> str:
    """Drop a leading caption fragment left by cite-boundary extraction ('Ins. Co., the Court held ...'
    -> 'the Court held ...'). Only a short, verb-less fragment before the first ', <article>' is removed."""
    import re as _re
    m = _re.match(r"^([^,.;]{1,40}(?:\.\s?[A-Z][a-z]{0,4}\.?){0,6}),\s+(?=(?:the|The|a|A|an|An|this|This|that|That)\b)", prop or "")
    if not m:
        return prop or ""
    head = m.group(1)
    # A caption FRAGMENT is a party-name tail ('Ins. Co.', 'Nat. Mut. Cas. Ins. Co.'): abbreviations /
    # capitalised nouns only. A real lead-in ('Under Kelly', 'In Rancosky', 'As the court held') starts
    # with a preposition/participle or carries a verb -- keep it. Over-strip lost 'Under Kelly,' (2026-09-27).
    if _re.match(r"(?i)^(?:under|in|per|as|see|according|citing|because|although|while|when|after|before)\b", head):
        return prop or ""
    if _re.search(r"(?i)\b(?:held|holds|is|are|was|were|has|have|does|did|rejected|stated|found|noted)\b", head):
        return prop or ""
    return (prop or "")[m.end():]


def _short_case_name(full: str) -> str:
    """'BIRTH CENTER v. The ST. PAUL COMPANIES' -> 'Birth Center'; first party, title-cased,
    articles and entity suffixes dropped. '' if there is no 'v.'."""
    import re as _re
    if not full or " v. " not in full:
        return ""
    first = full.split(" v. ")[0].strip()
    first = _re.sub(r"^(?:The|In re|Estate of)\s+", "", first, flags=_re.I)
    first = _re.sub(r",?\s+(?:Inc|Co|Corp|LLC|L\.L\.C|Ltd|LP|L\.P|Companies|Company|Ins|Insurance)\.?$", "", first, flags=_re.I).strip(" ,")
    if first.upper() == first:
        first = " ".join(w.capitalize() for w in first.split())
    return first


def _forum_code(slug: str) -> str:
    """Canonical court code of the case's forum from case_map.json (PA_CP_PHILA, EDPA, ...)."""
    import json as _json
    try:
        cm = _json.loads((ROOT / "cases_index" / "case_map.json").read_text(encoding="utf-8"))
        e = cm.get(slug) or {}
        for k in ("case_court", "court"):
            v = str(e.get(k) or "").strip()
            if v and v.upper() != "TBD":
                return v.upper()
    except Exception:
        pass
    return ""


def _fold_omitted_controlling_authority(slug: str, block: str, doc: str = "") -> str:
    """For each authority the movant cites: later opinions IN CORPUS that cite it and
    limit the movant's proposition, from a court that binds THIS forum, which the movant
    did not cite. Deterministic discovery (who-cites-whom + court from header); the model
    only confirms the limit and must quote it verbatim from the later opinion."""
    block = _strip_fold_section(block, "OMITTED CONTROLLING AUTHORITY")
    import re as _re, json as _json
    theirs = _their_authorities(slug) or {}
    if not theirs:
        return block
    adverse_txt = _adverse_text(slug) or ""
    forum = _forum_code(slug)
    binds = _BINDS_EDPA if forum.startswith("EDPA") or forum.startswith("FED") else _BINDS_PA_CCP
    try:
        con = _corpus_connect()
    except CorpusLocked:
        raise
    except Exception:
        return block
    cur = con.cursor()
    their_cites_flat = " ".join(adverse_txt.split())
    found, checked = [], 0
    for cite, name in theirs.items():
        # the movant's proposition = the sentence the cite sits in
        prop = ""
        pos = adverse_txt.find(cite)
        if pos >= 0:
            s0 = max(adverse_txt.rfind(". ", 0, pos), adverse_txt.rfind("\n\n", 0, pos), 0)
            s1 = adverse_txt.find(". ", pos)
            _st = s0 + 2 if s0 > 0 else 0
            prop = " ".join(adverse_txt[_st: (s1 + 1) if s1 > 0 else pos + 300].split())[:600]
            # a sentence that opens mid-citation ("Co., the Pennsylvania Supreme Court ...") -- back up to the case name
            if _re.match(r"^[A-Z][a-z]{0,6}\.,\s", prop):
                _b = adverse_txt.rfind(". ", 0, max(0, s0 - 1))
                prop = " ".join(adverse_txt[(_b + 2 if _b > 0 else 0): (s1 + 1) if s1 > 0 else pos + 300].split())[:700]
        # later opinions that cite this case (by reporter cite in their text)
        try:
            cur.execute("SELECT DISTINCT source FROM chunk_text WHERE text LIKE ? LIMIT 40",
                        ("%" + cite + "%",))
            srcs = [r[0] for r in cur.fetchall() if r and r[0]]
        except Exception:
            srcs = []
        own = _full_opinion(cite, name)
        own_src = own[0] if own else ""
        for src in srcs:
            if src == own_src or not src:
                continue
            checked += 1
            cur.execute("SELECT text FROM chunk_text WHERE source = ? ORDER BY chunk_id", (src,))
            parts = [r[0] for r in cur.fetchall() if r and r[0]]
            if not parts:
                continue
            head = _opinion_headmatter("\n".join(parts))
            court = _opinion_court(head)
            if court not in binds:
                continue
            # the later opinion's own reporter cite: from the source id when it carries one,
            # else the first cite in its headmatter; then check the movant did not cite it
            own_cite = _cite_from_source_id(src)
            head_cites = [m.group(0) for m in _CITE_RX.finditer(head) if m.group(0) != cite]
            later_cites = ([own_cite] if own_cite else []) + [c for c in head_cites if c != own_cite]
            if not later_cites or any(lc in their_cites_flat for lc in later_cites[:2]):
                continue   # unciteable, or the movant DID cite it -> not omitted
            _hm = " ".join(head.split())
            _k = _hm.find(later_cites[0])
            later_name = (_name_before_cite(_hm[max(0, _k - 200): _k]) if _k >= 0 else "") or \
                         _name_before_cite(_hm[:1200]) or ""
            # "787 A.2d 376 The BIRTH CENTER v. ..." — name follows the cite in headmatter
            if not later_name and _k >= 0:
                _nm = _re.match(r"\s*(?:The\s+)?([A-Z][A-Za-z.&'\u2019 ]{2,80}?\s+v\.\s+(?:The\s+)?[A-Z][A-Za-z.&'\u2019 ]{2,90}?)"
                               r"(?:,|\s+(?:Supreme|Superior|Commonwealth) Court|\.\s+(?:Supreme|Superior|Commonwealth|Argued|Decided))",
                               _hm[_k + len(later_cites[0]):])
                later_name = _nm.group(1).strip() if _nm else ""
                # title-case the reporter's ALL-CAPS caption
                if later_name and later_name.upper() == later_name:
                    later_name = " ".join(w if w in ("v.",) else w.capitalize() for w in later_name.split())
            # passages of the later opinion that discuss the earlier case
            pas = [" ".join(t.split()) for t in parts if cite in t]
            _hold_rx = _re.compile(r"(?i)\b(?:we hold|we conclude|we (?:therefore |accordingly )?(?:affirm|reverse|vacate)|"
                                   r"accordingly, we|order (?:is )?(?:affirmed|reversed)|we disagree)\b")
            hold = [" ".join(t.split()) for t in parts if _hold_rx.search(t) and cite not in t]
            pas_txt = "\n---\n".join(p[:1800] for p in (pas[:4] + hold[:3]))
            if not pas_txt:
                continue
            _short_later = _short_case_name(later_name)
            _short_earlier = _short_case_name(name)
            prompt = (f"{_OMIT_SYS}\n\nPARTY'S PROPOSITION FROM THE EARLIER CASE:\n{prop or '(none stated)'}\n\n"
                      f"EARLIER CASE: {name}, {cite}"
                      f"{' (SHORT NAME: ' + _short_earlier + ')' if _short_earlier else ''}\n\n"
                      f"LATER OPINION ({court}; {later_name + ', ' if later_name else ''}{later_cites[0]}"
                      f"{'; SHORT NAME: ' + _short_later if _short_later else ''}) PASSAGES CITING IT:\n{pas_txt}")
            try:
                raw = (_call_direct(prompt, max_tokens=420, timeout=240) or "").strip()
                raw = raw[raw.find("{"): raw.rfind("}") + 1]
                ans = _json.loads(raw) if raw else {}
            except CorpusLocked:
                raise
            except Exception:
                ans = {}
            rel = str(ans.get("relation", "")).upper().strip()
            if rel not in ("REJECTED", "LIMITED", "DISTINGUISHED"):
                continue
            q = " ".join(str(ans.get("quote") or "").split())
            # GUARDRAIL: verbatim from the later opinion. Compare alphanumerics only -- the
            # corpus carries markdown italics (_D'Ambrosio_), curly quotes and soft hyphens.
            _n = lambda x: _re.sub(r"[^a-z0-9]", "", str(x).lower())
            if len(_n(q)) < 20 or _n(q) not in _n(pas_txt):
                continue
            # heading: the model's, shape-guarded only (a verb, names the later case, one sentence)
            hd = " ".join(str(ans.get("our_heading") or "").split()).strip().rstrip(".")
            # shape guard: has a limiting verb and names the later case (anywhere). Failing that,
            # the floor is built from the two short names -- a heading is never absent, because the
            # assembler's default ("X does not defeat the claim") is the wrong voice for OUR authority.
            _ok_hd = (0 < len(hd) <= 170
                      and _re.search(r"(?i)\b(limit|narrow|confin|restrict|control|cabin|does not|did not|omit|reject|"
                                     r"forecl|held|holds|opposite|recogni|distinguish|bars?|precludes?)", hd)
                      and (not _short_later or _short_later.lower() in hd.lower()))
            if not _ok_hd:
                _verb = {"REJECTED": "rejects the movant's reading of", "LIMITED": "narrows",
                         "DISTINGUISHED": "distinguishes"}[rel]
                if _short_later and _short_earlier:
                    hd = f"{_short_later} {_verb} {_short_earlier}, and the movant does not cite it"
                elif _short_later:
                    hd = f"{_short_later} narrows the authority the movant relies on, and the movant does not cite it"
                else:
                    # no parseable short name: still never blank -- cite stands in for the name
                    hd = f"{later_cites[0]} {_verb} the authority the movant relies on, and the movant does not cite it"
            found.append({"earlier": f"{name}, {cite}" if name else cite, "later_name": later_name, "heading": hd, "relation": rel,
                          "later_cite": later_cites[0], "court": court, "quote": q,
                          "holds": " ".join(str(ans.get("what_it_holds") or "").split())[:400],
                          "prop": prop, "src": src})
    con.close()
    print(f"  [{slug}] OMITTED-AUTHORITY: {checked} later opinion(s) checked; {len(found)} binding later holding(s) against the movant's reading, not cited")
    if not found:
        return block
    _pretty = {"PA_SUPREME": "Supreme Court of Pennsylvania", "PA_SUPERIOR": "Superior Court of Pennsylvania",
               "PA_COMMONWEALTH": "Commonwealth Court of Pennsylvania", "US_SUPREME": "Supreme Court of the United States",
               "CA3": "Third Circuit"}
    out = ["", "### OMITTED CONTROLLING AUTHORITY", "",
           f"- The movant's authorities have been construed by later binding decisions the movant does not cite."]
    for f in found:
        _lead = (f"**{f['heading']}.** {f['later_name'] + ', ' if f['later_name'] else ''}{f['later_cite']}"
                 if f.get("heading") else
                 f"**{f['later_name'] + ', ' if f['later_name'] else ''}{f['later_cite']}**")
        line = (f"- {_lead} ({_pretty.get(f['court'], f['court'])}), "
                f"construing {f['earlier']}, which the movant cites for: \"{_clean_prop(f['prop'])[:260]}\" — "
                f"{f['holds']} The later court states: \"{f['quote']}\" "
                f"[{f['relation'].lower()}]")
        out.append(line + f" <!-- corpus: {f['src']} -->")
    out.append("- A party that cites an authority owes the Court the later controlling decision that "
               "limits it (Pa.R.P.C. 3.3(a)(2): duty to disclose directly adverse controlling authority). "
               "The omission is itself a defect in the filing, and the proposition falls with it.")
    out.append("")
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + out + lines[1:])
    return "\n".join(out) + "\n" + body


def _verify_their_characterizations(slug: str, block: str) -> str:
    """Check the movant's parentheticals against the opinions themselves.

    m0 was copying the movant's description of a case into its own ledger -- e.g. "UTPCPL
    pre-formation only", lifted verbatim from the movant's parenthetical for an opinion
    that was not in corpus at the time. Repeating opposing counsel's account of a case we
    have not read is how a brief adopts its opponent's framing, and it is the one thing
    the user's rule forbids absolutely: cite the source, never the summary.

    For every authority the movant characterizes, pull the opinion (fetching it if absent)
    and record what it actually says. Where the movant's account is materially narrower or
    broader than the opinion, that discrepancy is itself an argument.
    """
    if "WHAT THEIR AUTHORITIES ACTUALLY SAY" in (block or ""):
        return block
    import re as _re
    adverse_txt = _adverse_text(slug)
    if not adverse_txt:
        return block
    theirs = _their_authorities(slug) or {}
    ours = _our_pleaded_authorities(slug) or {}
    rows, checked = ["", "### WHAT THEIR AUTHORITIES ACTUALLY SAY", ""], 0
    for cite, name in theirs.items():
        if cite in ours:
            continue
        # the movant's own parenthetical for this authority
        # After the cite the movant writes court/date parentheticals, THEN the
        # descriptive one. Take the first parenthetical that is prose, not citation
        # furniture (court, year, "applying X law", page pin).
        claim = ""
        _pos = adverse_txt.find(cite)
        if _pos >= 0:
            _tail = adverse_txt[_pos: _pos + 700]
            for _p in _re.findall(r"\(([^()]{16,400})\)", _tail):
                _c = " ".join(_p.split())
                _cl = _c.lower()
                if _re.fullmatch(r"[a-z.\s]*\d{4}", _cl):          # (E.D. Pa. 2016)
                    continue
                if _cl.startswith("applying") or _cl.startswith("quoting") or \
                   _cl.startswith("citing") or _cl.startswith("internal") or \
                   len(_c.split()) < 4:
                    continue
                claim = _c
                break
        if not claim:
            continue
        holding = _holding_text(cite, name)
        if not holding and _PULL_MISSING and _pull_missing_authority(cite, name):
            holding = _holding_text(cite, name)
        if not holding:
            rows.append(f"- **{name}, {cite}.** The movant characterizes this authority "
                        f"as: \"{claim}\" The opinion is not in corpus and the movant's "
                        f"characterization is unverified; it is not adopted.")
            rows.append("")
            checked += 1
            continue
        prompt = (
            "You are verifying whether an opposing party's parenthetical accurately "
            "describes an opinion. You are given the parenthetical and the opinion text "
            "from the official reporter.\n\n"
            "Reply in exactly this form:\n"
            "VERDICT: ACCURATE or INCOMPLETE or MISLEADING\n"
            "ARGUMENT: <two or three sentences of FILING-GRADE prose stating what the "
            "opinion actually holds and what question it decided, and -- if the "
            "parenthetical is not accurate -- what it leaves out or overstates>\n\n"
            "Rules for ARGUMENT: write it as it would appear in a brief. Never mention "
            "the parenthetical, the verification, the excerpt, or what the supplied text "
            "does or does not contain. Never write 'it is accurate'. Describe the "
            "opinion and the record only. Consider especially the facts the case turned "
            "on -- a rule stated for one kind of conduct does not reach a different "
            "kind. Use only the text supplied. Never invent a holding.\n\n"
            f"AUTHORITY: {name}, {cite}\n\n"
            f"THE MOVANT'S PARENTHETICAL: \"{claim}\"\n\n"
            f"OPINION TEXT:\n{holding}\n\nVerification:")
        try:
            ans = " ".join((_call_direct(prompt, max_tokens=320, timeout=240) or "").split())
        except Exception:
            ans = ""
        if not ans:
            continue
        _verdict = _verifier_verdict(ans)
        _arg = re.sub(r"(?is)^.*?\bARGUMENT\s*:\s*", "", ans).strip()
        _arg = _strip_verifier_meta(_arg or ans)
        if _verdict == "ACCURATE":
            # the opponent's description survives -- saying so in our own brief only
            # strengthens their citation. Nothing to argue; drop it.
            print(f"  [{slug}] characterization OK (not argued): {name}, {cite}")
            continue
        if len(_arg) < 90:
            # nothing usable survived the meta strip; a stub point is worse than none
            print(f"  [{slug}] characterization unusable (dropped): {name}, {cite}")
            continue
        rows.append(f"- **{name}, {cite}.** The movant advances this authority for the "
                    f"proposition that {claim.rstrip('.')}. {_arg}")
        rows.append("")
        checked += 1
    if checked == 0:
        return block
    print(f"  [{slug}] CHARACTERIZATION CHECK: {checked} of the movant's parentheticals "
          f"verified against the opinions")
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + rows + lines[1:])
    return "\n".join(rows) + "\n" + body


def _attacked_counts(slug: str, doc: str) -> list:
    """Counts the movant actually attacks, from the analysis (not from a hardcode)."""
    out = []
    for m in re.finditer(r"movant attacks[^.]{0,160}", doc or "", re.I):
        for _c in re.findall(r"\b([IVX]{1,5})\b(?=\s*\(|\s*,|\s*and|\s*$)", m.group(0)):
            if _c not in out:
                out.append(_c)
    return out


def _count_elements(doc: str) -> dict:
    """{count_roman: (title, [elements])} from m0's own elements section."""
    out = {}
    m = re.search(r"^##\s*CAUSES OF ACTION AND ELEMENTS(.*?)(?=^## |\Z)", doc or "",
                  re.S | re.M)
    if not m:
        return out
    body = m.group(1)
    for cm in re.finditer(
            r"^###\s*Count\s+([IVX]+)\s*:\s*([^\n]+)\n(.*?)(?=^###\s*Count|\Z)",
            body, re.S | re.M):
        roman, title, blk = cm.group(1), cm.group(2).strip(), cm.group(3)
        els = re.findall(r"^\s*\d+\.\s+([^\n]{6,300})", blk, re.M)
        # the element list precedes the pleading-ready paragraphs
        pr = blk.find("Pleading-Ready")
        if pr > 0:
            els = re.findall(r"^\s*\d+\.\s+([^\n]{6,300})", blk[:pr], re.M)
        if els:
            out[roman] = (title, [" ".join(e.split()) for e in els])
    return out



def _pleaded_counts(slug: str, doc: str) -> dict:
    """{roman: name} for every count the FILED complaint actually pleads.

    Read from the analysis's own prose ("common law bad faith (Count III)",
    "Count V (UTPCPL)"), so it follows the pleading rather than a hardcoded list.
    """
    out = {}
    text = doc or ""
    _cand0 = {}
    for m in re.finditer(r"([A-Za-z][A-Za-z'\u2019 &.\-]{3,60}?)\s*\(Count\s+([IVX]+)\)", text):
        nm, roman = " ".join(m.group(1).split()), m.group(2)
        nm = re.sub(r"^(?:and|or|the|a|an)\s+", "", nm, flags=re.I).strip(" ,;:")
        if nm:
            _cand0.setdefault(roman, []).append(nm)
    _cand = dict((k, list(v)) for k, v in _cand0.items())
    for m in re.finditer(r"Count\s+([IVX]+)\s*\(([^)]{3,60})\)", text):
        roman, nm = m.group(1), " ".join(m.group(2).split())
        nm = re.sub(r"^(?:and|or|the|a|an)\s+", "", nm, flags=re.I).strip(" ,;:")
        # a name is a label, not a clause about what a party argues
        if re.search(r"(?i)\b(movant|defendant|plaintiff|relies|argues|does not|"
                     r"applies|asserts|contends|simultaneously)\b", nm):
            continue
        _cand.setdefault(roman, [])
        if len(nm.split()) > 9:
            continue
        _cand.setdefault(roman, []).append(nm)
    _DOCTRINE = ("unjust enrichment", "conversion", "breach of contract", "bad faith",
                 "implied covenant", "utpcpl", "unfair trade", "consumer protection",
                 "negligen", "misrepresent", "fraud", "restitution", "money had",
                 "declaratory", "quantum meruit", "replevin", "trespass")
    for roman in list(_cand):
        _cand[roman] = [
            _n for _n in _cand[roman]
            if not re.search(r"(?i)\b(movant|relies|argues|does not|asserts|contends|"
                             r"simultaneously|inapplicab)\b", _n)
            and len(_n.split()) <= 9] or _cand[roman]
    for roman, names in _cand.items():
        def _score(_n):
            _l = _n.lower()
            _s = 0
            if any(_d in _l for _d in _DOCTRINE):
                _s -= 100                      # strongly prefer a doctrine label
            if re.match(r"(?i)^(plaintiff|defendant|the )", _n):
                _s += 50                       # not a cause of action
            if not re.match(r"(?i)^[a-z0-9]", _n.strip()):
                _s += 20                       # fragment like "d the inapplicability"
            return (_s, len(_n))
        out.setdefault(roman, min(names, key=_score))
    # OUR pleaded label governs over any name scraped from prose that may repeat the
    # movant's substituted label for the same count.
    for m in re.finditer(
            r"Count\s+([IVX]+)\s+is\s+pleaded\s+as\s+([A-Z][^.\n]{4,90})", text):
        out[m.group(1)] = " ".join(m.group(2).split()).strip(" .,;:*")
    return out


def _audit_element_completeness(slug: str, doc: str) -> list:
    """Counts the complaint pleads that the elements section never builds a chain for.

    An opposition cannot be airtight on a count whose elements were never stated: the
    demurrer is aimed at the count as pleaded, so every pleaded count needs its element
    chain in the analysis.
    """
    pleaded = _pleaded_counts(slug, doc)
    have = _count_elements(doc)
    return [(r, n) for r, n in sorted(pleaded.items()) if r not in have]


def _build_missing_element_chains(slug: str, doc: str) -> str:
    """Build an element chain for every pleaded count that lacks one.

    Am Jur discipline: element -> record proof -> authority. A count under demurrer with
    no stated elements is an open door; this closes it. Corpus-cited only -- the prompt
    forbids inventing authority, and an element with no corpus support is written as
    NOT IN CORPUS rather than papered over.
    """
    missing = _audit_element_completeness(slug, doc)
    if not missing:
        return doc
    facts = ""
    try:
        blk = _current_block(doc, _SECTION_HEADERS["rebuttal"]) or ""
        facts = _record_facts_text(slug, blk) or ""
    except Exception:
        pass
    built = []
    for roman, name in missing:
        _auth_txt, _auth_cite = _element_authority_context(name)
        if _auth_txt:
            _AUTH_BLOCK = ("CONTROLLING AUTHORITY STATING THIS TEST (from corpus -- "
                           "state the elements as THIS text states them):\n"
                           + _auth_txt + ("\n[" + _auth_cite + "]" if _auth_cite else "")
                           + "\n\n")
            print(f"  [{slug}] ELEMENT AUTHORITY: Count {roman} grounded"
                  + (f" ({_auth_cite})" if _auth_cite else ""))
        else:
            _AUTH_BLOCK = ("CONTROLLING AUTHORITY: none found in corpus for this cause "
                           "of action. State only what the statute's own text supplies, "
                           "and name the statute.\n\n")
            print(f"  [{slug}] ELEMENT AUTHORITY: Count {roman} NOT grounded "
                  f"-- elements restricted to statutory text")
        prompt = (
            "You are stating the ELEMENTS of a cause of action for a Pennsylvania "
            "state-court opposition to preliminary objections.\n\n"
            f"COUNT {roman}: {name}\n\n"
            "ESTABLISHED FACTS OF THIS RECORD:\n" + facts[:2600] + "\n\n"
            + _AUTH_BLOCK
            + "Write, in this exact markdown shape and nothing else:\n"
            f"### Count {roman}: {name}\n\n"
            "**Elements:** (<controlling authority that states this test>)\n"
            "1. <element, one clause, EXACTLY as the authority above states it>\n"
            "2. <element>\n"
            "(state ONLY the elements the authority states -- if it states two, write "
            "two; never pad to a count)\n\n"
            "**Pleading-Ready Paragraphs:**\n\n"
            "1. <declarative averment tying THIS record's facts to element 1, with the "
            "date and the document that proves it>\n"
            "2. <same for element 2>\n\n"
            "RULES: use ONLY the facts above -- invent no fact, no date, no amount. "
            "State NO element that the CONTROLLING AUTHORITY above does not state: no "
            "deadline, no notice period, no threshold of your own invention. If the "
            "authority block is empty, write the elements ONLY if the statute's own text "
            "supplies them, and name it. Each element must rest on a DIFFERENT "
            "record fact where the record allows. Do not mention the defendant's "
            "characterization of the claim; state the count as pleaded.")
        try:
            ans = _call_direct(prompt, max_tokens=620, timeout=300)
        except Exception:
            ans = ""
        ans = (ans or "").strip()
        if "**Elements:**" not in ans:
            print(f"  [{slug}] ELEMENT CHAIN: Count {roman} — model returned no usable "
                  f"chain; left reported as a gap")
            continue
        ans = _strip_dangling_shortform(ans)
        built.append(ans)
        print(f"  [{slug}] ELEMENT CHAIN BUILT: Count {roman} ({name})")
    if not built:
        return doc
    m = re.search(r"^##\s*CAUSES OF ACTION AND ELEMENTS.*?(?=^## |\Z)", doc, re.S | re.M)
    if not m:
        return doc
    section = m.group(0).rstrip() + "\n\n" + "\n\n".join(built) + "\n\n"
    return doc[:m.start()] + section + doc[m.end():]



_VERIFIER_META = (
    "the opinion text provided", "the text provided", "the provided text",
    "the opinion text does not", "does not contain any information",
    "the parenthetical", "the movant describes", "the movant characterizes",
    "it is accurate", "is accurate.", "accurately identifies",
    "accurately describes", "the excerpt", "based on the text supplied",
    "the supplied text", "no information about the date",
)


def _strip_verifier_meta(text: str) -> str:
    """Drop sentences that talk ABOUT the verification instead of making the argument.

    Filing-grade prose describes what the opinion held and what it did not reach. A
    sentence grading the opponent's parenthetical, or noting what the supplied excerpt
    did or did not contain, is working scratch and never belongs in a filed document.
    """
    # the model sometimes echoes a prompt label back ("... context. AUTHORITY: Toy v. Metro.") --
    # cut at the first echoed label; everything after it is the prompt, not the argument
    text = re.split(r"\s*(?:AUTHORITY|THE MOVANT'S PARENTHETICAL|OPINION TEXT|VERIFICATION|VERDICT|ARGUMENT)\s*:",
                    text or "", maxsplit=1)[0]
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())):
        low = sent.lower()
        if any(mk in low for mk in _VERIFIER_META):
            continue
        out.append(sent)
    return " ".join(out).strip()


def _verifier_verdict(text: str) -> str:
    """ACCURATE | ADVERSE | UNKNOWN -- whether the movant's parenthetical survives."""
    t = " ".join((text or "").split())
    m = re.search(r"(?im)^\s*VERDICT\s*:\s*([A-Z]+)", t)
    if m:
        v = m.group(1).upper()
        if v.startswith("ACCU"):
            return "ACCURATE"
        if v.startswith(("MISLEAD", "INCOMP", "OVERBROAD", "INAPP")):
            return "ADVERSE"
    low = t.lower()
    if re.search(r"\b(misleading|incomplete|overbroad|does not reach|mischaracteri|"
                 r"overstates|not support)\b", low):
        return "ADVERSE"
    if re.search(r"\b(is accurate|accurately (identifies|describes|states)|"
                 r"fairly describes)\b", low):
        return "ACCURATE"
    return "UNKNOWN"


def _hashmap_row(name: str) -> dict:
    """The law_hashmap row for a claim: {elements, cite, trap, lever, authorities}.

    obsidian_sync/law_hashmap/claim-<slug>.md is the deterministic claim -> law layer,
    keyed by a pipe-delimited CLAIM PATTERN ("bad faith|8371"). Every authority in it
    was re-verified against the corpus at build time and carries a [corpus: <source>]
    tag, plus a "Ready-to-plead" sentence stating the test in filing-ready form.

    This is the vault-as-pointer layer the engine was designed around. It is read
    FIRST, before any other source, because it is the only one whose citations were
    machine-verified against corpus text.
    """
    import glob, os, re as _re
    probe = " ".join((name or "").split()).lower()
    if not probe:
        return {}
    hm_dir = ROOT / "obsidian_sync" / "law_hashmap"
    if not hm_dir.is_dir():
        return {}

    best, best_score = None, 0
    for path in glob.glob(str(hm_dir / "claim-*.md")):
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        m = _re.search(r"(?m)^#\s*CLAIM PATTERN:\s*`([^`]+)`", txt)
        if not m:
            continue
        # a pattern is a pipe-delimited keyword set; score how many keys the claim hits
        keys = [k.strip().lower() for k in m.group(1).split("|") if k.strip()]
        score = 0
        for k in keys:
            if k and k in probe:
                # a statute number ("8371", "201-") is a far stronger signal than a word
                score += 3 if _re.search(r"\d", k) else 2
        if score > best_score:
            best, best_score = txt, score
    if not best or best_score < 2:
        return {}

    out = {"elements": "", "cite": "", "trap": "", "lever": "", "authorities": []}

    # Ready-to-plead states the test in filing-ready words; the FIRST one under the
    # highest authority is the controlling statement.
    for am in _re.finditer(
            r"(?ms)^###\s+(?P<cite>[^\n]+?)\s*\n(?P<body>.*?)(?=^###\s|^##\s|\Z)", best):
        body, cite = am.group("body"), am.group("cite").strip()
        verified = bool(_re.search(r"\*\*VERIFIED\*\*", body))
        corpus = ""
        cm = _re.search(r"\[corpus:\s*([^\]]+)\]", body)
        if cm:
            corpus = cm.group(1).strip()
        rp = _re.search(r"(?ms)\*\*Ready-to-plead:\*\*\s*(.+?)(?=\n\s*-\s\*\*|\n\s*\n|\Z)", body)
        ready = " ".join(rp.group(1).split()) if rp else ""
        pt = _re.search(r"(?m)\*\*Point:\*\*\s*(.+)$", body)
        out["authorities"].append({
            "cite": cite, "verified": verified, "corpus": corpus,
            "point": (pt.group(1).strip() if pt else ""), "ready": ready,
        })
        # prefer a VERIFIED authority that actually states the elements
        if ready and not out["elements"] and verified:
            out["elements"], out["cite"] = ready, cite

    if not out["elements"]:
        for a in out["authorities"]:
            if a["ready"]:
                out["elements"], out["cite"] = a["ready"], a["cite"]
                break

    tm = _re.search(r"(?ms)^##\s*TRAP[^\n]*\n(.*?)(?=^##\s|\Z)", best)
    if tm:
        out["trap"] = " ".join(tm.group(1).split())[:600]
    lm = _re.search(r"(?ms)^##\s*DISCRETION-EXPOSURE[^\n]*\n(.*?)(?=^##\s|\Z)", best)
    if lm:
        out["lever"] = " ".join(lm.group(1).split())[:600]
    return out


def _vault_element_note(name: str) -> tuple:
    """(elements_text, cite) from the Obsidian vault's authority notes.

    obsidian_sync/definitions_learned/ holds one structured note per authority, with a
    "## Key Provisions" block that states the test as numbered elements plus a "## Source"
    citation. That is a curated statement of the law -- infinitely better than LIKE-
    scraping raw chunk text, which produced a brief's question-presented for § 8371 and
    an HVAC spec for conversion. Vault first; corpus scrape only as fallback.
    """
    import glob, os
    probe = " ".join((name or "").split())
    if not probe:
        return "", ""
    # tokens that identify the authority: a statute number, or the cause-of-action words
    nums = re.findall(r"\d{3,5}", probe)
    words = [w.lower() for w in re.split(r"[^A-Za-z]+", probe)
             if len(w) > 3 and w.lower() not in ("count", "violation", "claim", "the")]
    vault = str(ROOT / "obsidian_sync" / "definitions_learned")
    if not os.path.isdir(vault):
        return "", ""
    best, best_score, best_cite = "", 0, ""
    # A note must match on a DISTINCTIVE token, not a generic legal word. Without this,
    # "Zorbulax Flangewright Tort" matched a generic tort note and would have shipped
    # that note's elements as though they were this count's -- the exact failure mode
    # being fixed. Generic words cannot carry a match on their own.
    _GENERIC = {"tort", "torts", "claim", "claims", "action", "actions", "damage",
                "damages", "law", "laws", "legal", "civil", "common", "general",
                "liability", "duty", "pennsylvania", "federal", "state", "statute"}
    distinctive = [w for w in words if w not in _GENERIC]
    if not nums and not distinctive:
        return "", ""
    for path in glob.glob(os.path.join(vault, "*.md")):
        base = os.path.basename(path).lower()
        score = sum(2 for n in nums if n in base) + sum(1 for w in distinctive if w in base)
        if score < 1:
            continue
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        # The vault is not uniform: elements live under "## Key Provisions", a nested
        # "### Elements of X Under Pennsylvania Law", or "## Core Elements". Prefer the
        # most specific ELEMENTS heading at any depth, then fall back to broader blocks.
        blk = ""
        m = re.search(r"(?ms)^#{2,4}\s*(?:Core\s+)?Elements\b[^\n]*\n(.*?)(?=^#{2,4}\s|\Z)",
                      txt)
        if m and m.group(1).strip():
            blk = m.group(1).strip()
        if not blk:
            m = re.search(r"(?ms)^##\s*Key Provisions\s*\n(.*?)(?=^##\s|\Z)", txt)
            blk = m.group(1).strip() if m else ""
        if not blk:
            m = re.search(r"(?ms)^##\s*Definition\s*\n(.*?)(?=^##\s|\Z)", txt)
            blk = m.group(1).strip() if m else ""
        if not blk:
            continue
        # must actually state a test, not just discuss the doctrine
        if not re.search(r"(?i)\b(?:test|element|must\s+(?:prove|show|establish|plead)|"
                         r"prong)\b", blk) and not re.match(r"\s*(?:\d\.|[-*])", blk):
            continue
        cite = ""
        for head in ("Source", "Citation", "Sources"):
            cm = re.search(r"(?ms)^##\s*" + head + r"\s*\n(.*?)(?=^##\s|\Z)", txt)
            if cm and cm.group(1).strip():
                cite = " ".join(cm.group(1).split())[:220]
                break
        if score > best_score:
            best, best_score, best_cite = blk[:1400], score, cite
    return best, best_cite


def _element_authority_context(name: str) -> tuple:
    """(authority_text, cite) stating the elements of THIS cause of action.

    Vault authority notes first (curated, citation-carrying); raw corpus scrape only
    when the vault has no note. m0 must never state elements from model recall: that is
    how "within 60 days of receipt of proof of loss" became an element of 42 Pa.C.S.
    § 8371, which has no such element. Returns ("", "") when neither source is clean --
    the caller then refuses to invent.
    """
    # 1. law_hashmap: the O(1) claim -> authority row, citations corpus-VERIFIED at
    #    build time. This layer existed but nothing read it, so m0 was stating elements
    #    without it and got Rancosky's reporter cite wrong.
    row = _hashmap_row(name)
    hm_elements = row.get("elements", "")
    # A hashmap row may carry only a DEFENSE-DEFEATER and no element statement: the
    # conversion row holds Bruno (gist-of-the-action) and nothing stating conversion's
    # own elements. Pleading Bruno as the cause of action would state the wrong test,
    # so the row's element text is accepted only if it actually speaks to THIS claim.
    if hm_elements:
        _claim_words = [w for w in re.split(r"[^A-Za-z]+", (name or "").lower())
                        if len(w) > 3 and w not in ("count", "violation", "claim",
                                                    "statutory", "under", "this")]
        _low = hm_elements.lower()
        # A statute number in the claim name is the strongest on-point signal: the
        # § 8371 row states the test without ever using the words "bad faith", and a
        # word-only check wrongly rejected it and fell through to a note whose Source
        # line carried a WRONG reporter cite. Match on the statute first.
        _nums = [n for n in re.findall(r"\d{3,5}", name or "")]
        _on_point = (any(n in hm_elements for n in _nums)
                     or (not _claim_words) or any(w in _low for w in _claim_words))
        if not _on_point:
            hm_elements = ""      # fall through to the curated note
    if hm_elements:
        txt = hm_elements
        if row.get("trap"):
            txt += "\n\nKNOWN TRAP (do not let the count be side-stepped this way): " + row["trap"]
        return txt, (row.get("cite") or "")

    # 2. curated authority notes. When the hashmap had a TRAP but no usable element
    #    statement, keep the trap: it is the encoded side-step for this claim.
    vt, vc = _vault_element_note(name)
    if vt:
        if row.get("trap"):
            vt += "\n\nKNOWN TRAP (do not let the count be side-stepped this way): " + row["trap"]
        return vt, vc
    # NO CORPUS FALLBACK for element statements. LIKE-scraping raw chunk text cannot
    # tell "the elements of THIS cause of action" from a jury instruction in an
    # unrelated case, an Am Jur form for legal malpractice, or an EMTALA holding --
    # all three were returned for our own counts. A wrong element set is worse than a
    # reported gap, because it ships to a court as though it were the law. When the
    # vault has no authority note, m0 is told to use the statute's own text and the
    # gap is reported so a note can be written.
    return "", ""


def _element_corpus_fallback_DISABLED(name: str) -> tuple:
    """Kept for reference only: the corpus-scrape path, disabled above.

    Retained so the next attempt at machine-grounded elements starts from what was
    already tried and why it failed, rather than rebuilding the same scraper.
    """
    probe = " ".join((name or "").split())
    if not probe:
        return "", ""
    try:
        import sqlite3
        con = _corpus_connect()
        cur = con.cursor()
        # look for an explicit elements/test statement tied to this cause of action
        words = [w for w in re.split(r"[^A-Za-z\u00a7\d.]+", probe) if len(w) > 3][:4]
        hits = []
        # One AND-of-all-words LIKE misses "Breach of Contract" whenever the treatise
        # writes "elements of a breach of contract claim are" with words in between.
        # Probe the HEAD term instead, and let the scorer below reject off-topic text.
        head = words[0] if words else probe
        second = words[1] if len(words) > 1 else ""
        for pat in ("elements of", "two-part test", "plaintiff must show",
                    "plaintiff must prove", "in order to recover",
                    "to state a claim", "a plaintiff must"):
            try:
                if second:
                    cur.execute(
                        "SELECT text FROM chunk_text WHERE text LIKE ? AND text LIKE ? "
                        "AND text LIKE ? LIMIT 8",
                        ("%" + head + "%", "%" + second + "%", "%" + pat + "%"))
                else:
                    cur.execute(
                        "SELECT text FROM chunk_text WHERE text LIKE ? AND text LIKE ? "
                        "LIMIT 8", ("%" + head + "%", "%" + pat + "%"))
                hits.extend(r[0] for r in cur.fetchall())
            except CorpusLocked:
                raise
            except Exception:
                continue
        con.close()
    except Exception:
        return "", ""
    best, best_cite = "", ""
    # A sentence "containing the word must" is not a statement of elements: the naive
    # longest-match picked a brief's question-presented for § 8371 and an HVAC spec for
    # conversion. Require (a) an elements/test PHRASE, (b) legal-register vocabulary,
    # and (c) topical overlap with the cause of action being stated.
    _TEST_PHRASE = re.compile(
        r"(?i)\b(?:the\s+elements\s+of|elements\s+of\s+(?:a|an|the)|"
        r"(?:two|three|four|five)[- ]part\s+test|"
        r"in\s+order\s+to\s+recover|to\s+(?:state|establish|prevail\s+on)\s+"
        r"(?:a|an|the)\s+claim|plaintiff\s+must\s+(?:show|prove|establish|plead)|"
        r"a\s+plaintiff\s+must\s+(?:show|prove|establish|plead))\b")
    _LEGAL = ("plaintiff", "claim", "cause of action", "defendant", "insurer",
              "liability", "damages", "court", "recover")
    _NONLEGAL = ("figure", "sensor", "temperature", "contractor employee",
                 "system of records", "hrf-", "specimen", "apparatus", "voltage")
    topic = {w.lower() for w in re.split(r"[^A-Za-z]+", probe) if len(w) > 4}
    for t in hits:
        flat = " ".join((t or "").split())
        for m in re.finditer(r"[^.;]{60,420}[.;]", flat):
            sent = m.group(0).strip()
            low = sent.lower()
            if not _TEST_PHRASE.search(sent):
                continue
            if any(b in low for b in ("table of", "index", "see also", "editor's note")):
                continue
            if any(b in low for b in _NONLEGAL):
                continue
            # a question-presented ("DID THE LOWER COURT ERR...") states no rule
            if sent.isupper() or low.startswith(("did ", "whether ")) or "?" in sent:
                continue
            # A LITIGANT'S ARGUMENT is not authority. "X has moved to dismiss, arguing
            # that..." reports what a party asserted; adopting it as the element test
            # would let an opponent's brief define our own count.
            if re.search(r"(?i)\b(?:ha[sd]\s+moved|moves?|argu(?:es|ed|ing)|contends?|"
                         r"asserts?|maintains?|urges?|alleges?)\b", sent) and not \
               re.search(r"(?i)\b(?:we\s+(?:adopt|hold)|the\s+court\s+(?:held|holds)|"
                         r"it\s+is\s+(?:well\s+)?settled)\b", sent):
                continue
            if not any(k in low for k in _LEGAL):
                continue
            # the sentence must be ABOUT this cause of action
            if topic and not (topic & {w.lower() for w in re.split(r"[^A-Za-z]+", sent)}):
                continue
            if len(sent) > len(best):
                best = sent
                cm = re.search(r"\b\d{1,3}\s+A\.\d[a-z]?\s+\d{1,4}\b|"
                               r"\b\d{1,3}\s+Pa\.[A-Za-z. ]{0,12}\d{1,4}\b", flat)
                best_cite = cm.group(0) if cm else ""
    return best[:900], best_cite


def _authority_element_statements(slug: str) -> list:
    """Every 'PLEAD:' statement m0 derived from controlling authority, across the
    case's stubs. These are the law as the corpus states it -- the yardstick a
    hand-written element list must answer to.

    Agnostic: no cause-of-action vocabulary, no jurisdiction assumptions. Any count
    of any kind is checked against the authority m0 pulled for it.
    """
    out = []
    d = CASES / str(slug or "") / "drafts"
    if not d.exists():
        return out
    for p in sorted(d.rglob("*.md")):
        if "_M1_DRAFT" in p.name or "/.backups/" in p.as_posix():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in re.finditer(r"(?m)^\s*-\s*PLEAD:\s*(.+?)$", txt):
            st = " ".join(m.group(1).split())
            if len(st) > 40:
                out.append(st)
    return out


def _element_contradicts_authority(element: str, statements: list) -> str:
    """Return the authority statement that CONTRADICTS this element, or "".

    An element is suspect when it asserts a concrete, checkable requirement -- a
    numeric threshold, a deadline, a named precondition -- that NO authority
    statement for the case supports. Enumerating a requirement the controlling
    authority does not impose is how a draft ends up pleading the wrong test.
    """
    el = " ".join(str(element or "").split())
    if len(el) < 12:
        return ""
    # the checkable assertions: "within 60 days", "$500", "30-day", percentages
    quant = re.findall(r"(?i)\b(?:within\s+)?\d+[\-\s]?(?:days?|months?|years?|%|"
                       r"percent|hours?)\b", el)
    if not quant:
        return ""
    joined = " ".join(statements).lower()
    for q in quant:
        # the SAME quantity must appear somewhere in the authority m0 pulled
        num = re.search(r"\d+", q)
        if not num:
            continue
        unit = re.search(r"(?i)(days?|months?|years?|%|percent|hours?)", q)
        u = (unit.group(1).lower().rstrip("s") if unit else "")
        if re.search(r"\b" + num.group(0) + r"\b[\s\-]*" + u, joined):
            return ""
    # no authority states this threshold -- report the closest authority on point
    ew = [w for w in re.findall(r"[a-z]{5,}", el.lower())]
    best, hb = "", 0
    for st in statements:
        h = sum(1 for w in set(ew) if w in st.lower())
        if h > hb:
            best, hb = st, h
    return best if hb >= 2 else (statements[0] if statements else "")


def _fold_element_coverage(slug: str, block: str, doc: str = "") -> str:
    """AM JUR ELEMENT CHAINS: every element of every attacked count, proof + authority.

    Structural: the opposition is organized by OUR counts and their elements, so no
    element is left unanswered because the movant chose not to raise it.
    """
    _existing_sec = ""
    if "AM JUR ELEMENT CHAINS" in (block or ""):
        _m_ex = re.search(r"\n?### AM JUR ELEMENT CHAINS.*?(?=\n### |\Z)",
                          block, re.S)
        _existing_sec = _m_ex.group(0) if _m_ex else ""
    whole = doc or block or ""
    try:
        whole = (CASES / slug / "CASE_FACTS.md").read_text(encoding="utf-8")
    except Exception:
        pass
    counts = _count_elements(whole)
    _missing = _audit_element_completeness(slug, whole)
    if not counts and not _missing:
        return block
    attacked = _attacked_counts(slug, whole) or list(counts.keys())
    facts = _record_facts_text(slug, block) or ""
    rows, gaps = ["", "### AM JUR ELEMENT CHAINS — EVERY ELEMENT MATCHED TO RECORD PROOF", ""], []
    _miss_map = dict(_missing)
    def _chain_paras(_whole, _roman):
        _m = re.search(r"^###\s*Count\s+" + _roman + r"\s*:.*?(?=^###\s*Count|\Z)",
                       _whole, re.S | re.M)
        if not _m:
            return []
        _blk = _m.group(0)
        _pr = _blk.find("Pleading-Ready")
        if _pr < 0:
            return []
        return [" ".join(x.split()) for x in re.findall(
            r"^\s*\d+\.\s+([^\n]{30,600})", _blk[_pr:], re.M)]

    for roman in attacked:
        if roman not in counts:
            _nm = _miss_map.get(roman, "")
            if _nm:
                gaps.append(f"Count {roman} ({_nm}): the analysis states no elements for "
                            f"this count, yet the movant demurs to it")
                rows.append(f"- **Count {roman} ({_nm}) — ELEMENTS NOT STATED.** The "
                            f"analysis builds no element chain for this count. The "
                            f"opposition cannot be airtight on it until the elements are "
                            f"stated and matched to record proof.")
            continue
        title, els = counts[roman]
        _seg_parts = [f"- **Count {roman} ({title}) — every element is met on the "
                      f"face of the pleading.** A demurrer admits the pleading as "
                      f"written, so the only question is whether each element of this "
                      f"count is averred. Each is, and the averment that satisfies it "
                      f"follows."]
        _paras = _chain_paras(whole, roman)
        # The element list is scraped from the analysis prose, so a mis-stated element
        # propagates into every draft unchallenged. Check each one against the
        # authority m0 actually pulled for this case before building a chain on it.
        _auth_st = _authority_element_statements(slug)
        for i, el in enumerate(els, 1):
            _contra = _element_contradicts_authority(el, _auth_st)
            if _contra:
                gaps.append(f"Count {roman} element {i} states a requirement no "
                            f"authority of record imposes: \"{' '.join(str(el).split())[:90]}\"")
                _seg_parts.append(
                    f"Element {i} — ELEMENT NOT SUPPORTED BY AUTHORITY OF RECORD. The "
                    f"analysis states this element as \"{' '.join(str(el).split()).rstrip('.')}\", "
                    f"but the controlling authority pulled for this count states: "
                    f"{_contra[:300]} The element must be restated from that authority "
                    f"before any chain is built on it.")
                continue
            # record proof: the established fact sharing the most distinctive words
            stop = {"the", "a", "an", "of", "to", "and", "or", "its", "was", "were",
                    "plaintiff", "defendant", "insurer"}
            ew = [w for w in re.findall(r"[a-z]{4,}", el.lower()) if w not in stop]
            proof, pb = "", 0
            # the chain's own pleading-ready paragraph for THIS element is the proof
            if i <= len(_paras):
                proof, pb = _paras[i - 1], 99
            for line in ([] if pb else facts.splitlines()):
                ls = " ".join(line.split())
                if len(ls) < 30:
                    continue
                h = sum(1 for w in ew if w in ls.lower())
                if h > pb:
                    proof, pb = ls.lstrip("- ").strip(), h
            _el_txt = " ".join(str(el).split()).rstrip(".")
            if proof and pb >= 1:
                _seg_parts.append(f"Element {i} — {_el_txt}: "
                                  f"{' '.join(str(proof).split())}")
            else:
                gaps.append(f"Count {roman} element {i}: no record proof matched")
                _seg_parts.append(f"Element {i} — {_el_txt}: averred in the Complaint.")
        rows.append(" ".join(_seg_parts))
    if gaps:
        rows.append("")
        rows.append(f"- **Coverage report (not for filing):** {len(gaps)} element(s) "
                    f"lack matched proof or corpus authority: "
                    f"{'; '.join(gaps[:8])}. These are stated as gaps rather than "
                    f"papered over with unsupported assertions.")
    print(f"  [{slug}] AM JUR ELEMENT CHAINS: {len(attacked)} count(s) walked; "
          f"{len(gaps)} gap(s) reported")
    if _existing_sec:
        _pat = re.compile(r"(?m)^- \*\*Count ([IVX]+) ")
        _old_n = len(set(_pat.findall(_existing_sec)))
        _new_n = len(set(_pat.findall("\n".join(rows))))
        if _new_n <= _old_n:
            return block                      # existing section is as good or better
        print(f"  [{slug}] ELEMENT CHAIN REBUILD: {_old_n} -> {_new_n} count(s) covered")
        block = block.replace(_existing_sec, "")
    rows.append("")
    lines = (block or "").splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + rows + lines[1:])
    return "\n".join(rows) + "\n" + (block or "")


def _fold_breach_origin(slug: str, block: str) -> str:
    """Keep the breach dated to the RECORD event, not the opponent's paper trail.

    The analysis wrote that the insurer "breached the contract by failing to refund
    Plaintiff's unearned premiums after cancelling the policy on [the insurer's own
    cancellation date]". That silently adopts the opponent's framing: it dates the breach
    to a document THEY generated, years after the operative fact, and concedes that
    everything before their paper is not actionable.

    Structural, not case-specific: where the analysis dates the breach to a date the
    record assigns to the OPPONENT'S OWN instrument, and the record contains an earlier
    date for the event that ended the obligation, state the earlier origin expressly.
    """
    _existing = None
    if "WHEN THE BREACH BEGAN" in (block or ""):
        _m_ex = re.search(r"arose on ([A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4})",
                          block)
        _existing = _m_ex.group(1) if _m_ex else None
        if _existing is None:
            return block
        # fall through: recompute, and rewrite only if the stated origin is wrong
    import re as _re
    text = block or ""
    try:
        _whole = (CASES / slug / "CASE_FACTS.md").read_text(encoding="utf-8")
    except Exception:
        _whole = text
    scan = _whole if len(_whole) > len(text) else text
    # dates the record marks as the opponent's own instrument (cancellation/confirmation)
    theirs_dates = set(_re.findall(
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})[^.\n]{0,90}"
        r"(?:cancellation|cancelled|canceled|confirmation)", scan))
    theirs_dates |= set(_re.findall(
        r"(?:cancellation|cancelled|canceled|confirmation)[^.\n]{0,90}?"
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})", scan))
    _sale = set(_re.findall(
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})[^.\n]{0,120}"
        r"(?:sold|sale|transferred|no longer had|insurable interest)", scan, _re.I))
    _sale |= set(_re.findall(
        r"(?:sold|sale of|transferred)[^.\n]{0,120}?"
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})", scan, _re.I))
    # A retroactive cancellation is described as reaching BACK to the sale, so the sale
    # date lands in both sets. Exclude only a date the record states as the date of sale
    # in the operative sense ("sold ... on <date>"), never every date merely mentioned
    # near the word "sale".
    _sold_on = set(_re.findall(
        r"sold[^.\n]{0,80}?on\s+(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})",
        scan, _re.I))
    _sold_on |= set(_re.findall(
        r"(?:date of sale|sale date)[^.\n]{0,40}?"
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})", scan, _re.I))
    theirs_dates -= _sold_on
    if not theirs_dates:
        return block
    # a breach sentence anchored to one of those dates
    hits = []
    for d in sorted(theirs_dates):
        for m in _re.finditer(
                r"breach[^.\n]{0,400}?" + _re.escape(d) +
                r"|" + _re.escape(d) + r"[^.\n]{0,200}?breach", scan, _re.I):
            hits.append((d, " ".join(m.group(0).split())))
            break
    # also catch "proof of the sale ... on <their date>" -- the date proof was SENT
    # written as though it were the date of the sale itself
    for m in _re.finditer(
            r"(?:proof of (?:the )?sale|sale of the insured[^.\n]{0,60})[^.\n]{0,120}?"
            r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})", scan, _re.I):
        if m.group(1) in theirs_dates:
            hits.append((m.group(1), " ".join(m.group(0).split())))
    if not hits:
        return block
    # the earliest record date tied to the obligation ending (sale, transfer, disposal)
    def _parse(d: str):
        import datetime as _dt
        for f in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return _dt.datetime.strptime(d.strip(), f).date()
            except Exception:
                continue
        return None

    cands = set(_sale)
    # "sold the <thing> on <date>" / "<date> ... sold"
    for pat in (r"sold[^.\n]{0,120}?(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})",
                r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})[^.\n]{0,120}"
                r"(?:sold|no longer had|insurable interest)"):
        cands |= set(_re.findall(pat, scan, _re.I))
    # a date the record calls the date proof was PROVIDED is not the sale date
    _sent = set(_re.findall(
        r"(?:provid\w+|sent|submitt\w+|deliver\w+|proof of (?:the )?sale)[^.\n]{0,90}?"
        r"(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})", scan, _re.I))
    dated = sorted(
        ((_parse(c), c) for c in cands if c not in theirs_dates and _parse(c)),
        key=lambda x: x[0])
    # prefer the earliest sale date that is NOT merely the date proof was sent
    # An explicit "sold ... on <date>" is authoritative. Take the EARLIEST such date;
    # never let the "proof was sent" heuristic promote a later date over it.
    _explicit = set(_re.findall(
        r"sold[^.\n]{0,120}?\bon\s+(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})",
        scan, _re.I))
    _exp_dated = sorted(((_parse(c), c) for c in _explicit if _parse(c)), key=lambda x: x[0])
    origin = ""
    if _exp_dated:
        origin = _exp_dated[0][1]
    else:
        for _d, _c in dated:
            if _c not in _sent:
                origin = _c
                break
        if not origin and dated:
            origin = dated[0][1]
    if not origin:
        return block
    rows = ["", "### WHEN THE BREACH BEGAN", "",
            f"- The obligation to stop charging and to refund arose on {origin}, when the "
            f"insured interest ended — not on any later date appearing on the movant's "
            f"own cancellation paperwork. Every premium drawn after {origin} was drawn "
            f"without an insurable interest to support it.",
            f"- The movant's cancellation confirmations, generated on "
            f"{', '.join(sorted(theirs_dates))}, are the movant's admissions that the "
            f"coverage had ended; they are evidence of the breach, not the date it "
            f"began. A defendant does not set the accrual date of its own breach by "
            f"choosing when to paper the file.",
            ""]
    if _existing is not None:
        if _existing == origin:
            return block          # already correct
        print(f"  [{slug}] BREACH-ORIGIN FOLD: correcting stale origin {_existing} "
              f"-> {origin}")
        text = re.sub(r"\n?###\s*WHEN THE BREACH BEGAN.*?(?=\n###\s|\n##\s|\Z)", "",
                      text, flags=re.S)
        block = text
    else:
        print(f"  [{slug}] BREACH-ORIGIN FOLD: breach dated to the record event {origin}, "
              f"not the movant's paperwork")
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + rows + lines[1:])
    return "\n".join(rows) + "\n" + text


def _strip_dangling_shortform(text: str) -> str:
    """Remove a truncated case short-form left by name-deduplication.

    "Hart v. This record presents no equity action" -- the name was cut mid-citation.
    A party name followed by "v." and then ordinary prose (no second party, no reporter)
    is never valid citation form; emitting it into a filing looks like a fabricated cite.
    """
    import re as _re
    if not text:
        return text
    # "<Cap words> v. " NOT followed by a capitalized party + comma/reporter
    _RPTR = (r"\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.|U\.S\.|F\.\s?Supp\.?\s?\d?[a-z]{0,2}|"
             r"F\.\d[a-z]{0,2}|S\.Ct\.)\s+\d+")
    # "<Name> v. " is a real citation when a reporter cite follows within ~90 chars;
    # otherwise it is a truncated short-form left by name-deduplication.
    pat = _re.compile(
        r"\b[A-Z][A-Za-z.'\u2019\-]{1,30}(?:\s+[A-Z][A-Za-z.'\u2019\-]{1,30}){0,3}\s+v\.\s+"
        r"(?!.{0,90}?" + _RPTR + r")")
    out, guard = text, 0
    while guard < 8:
        m = pat.search(out)
        if not m:
            break
        tail = out[m.end():]
        # keep the sentence, drop only the dangling name fragment
        out = out[:m.start()] + tail[:1].upper() + tail[1:]
        guard += 1
    return out


_ROLE_SYS = (
    "You are reading ONE citation in an opposing party's motion. Decide the ROLE the "
    "movant uses this authority for. STANDARD = the procedural standard governing the "
    "motion itself (what a demurrer or motion takes as true, what the court may or may "
    "not consider, who bears the burden, the standard of review). MERITS = a substantive "
    "rule about a claim, defense, element, remedy, or doctrine. Answer with exactly one "
    "word: STANDARD or MERITS."
)
_ROLE_CACHE = {}


def _cite_role(slug: str, cite: str, name: str = "") -> str:
    """STANDARD | MERITS for how the movant deploys `cite`.

    The LLM reads the sentence window around the cite in the movant's own filing and
    selects; the deterministic guard accepts only a menu word and only when the cite is
    actually present in the filing. Unknown -> MERITS (the prior behaviour: distinguish).
    """
    key = (slug, cite)
    if key in _ROLE_CACHE:
        return _ROLE_CACHE[key]
    role = "MERITS"
    flat = " ".join((_adverse_text(slug) or "").split())
    k = flat.find(cite)
    if k >= 0:
        win = flat[max(0, k - 600): k + 250]
        prompt = (f"{_ROLE_SYS}\n\nAUTHORITY: {name or ''} {cite}\n\n"
                  f"PASSAGE FROM THE MOVANT'S FILING:\n{win}\n\nROLE:")
        try:
            ans = (_call_direct(prompt, max_tokens=8, timeout=120) or "").strip().upper()
        except Exception:
            ans = ""
        word = re.sub(r"[^A-Z]", "", ans.split()[0]) if ans.split() else ""
        if word in ("STANDARD", "MERITS"):
            role = word
    _ROLE_CACHE[key] = role
    return role


def _fold_distinctions(slug: str, block: str, doc: str = "") -> str:
    """One factual distinction per adverse authority, grounded in corpus + our record."""
    if "DISTINCTIONS ON THIS RECORD" in (block or ""):
        return block
    theirs = _their_authorities(slug) or {}
    ours = _our_pleaded_authorities(slug) or {}
    adverse = {c: n for c, n in theirs.items() if c not in ours}
    if not adverse:
        return block
    facts = _record_facts_text(slug, block)
    if not facts or len(facts) < 200:
        print(f"  [{slug}] DISTINCTIONS: skipped — record facts unavailable")
        return block

    rows = ["", "### DISTINCTIONS ON THIS RECORD", ""]
    made, empty = 0, []
    for cite, name in adverse.items():
        # a standard-of-review authority is invoked against the movant (flip-check),
        # never distinguished on the facts -- the distinction would be a non-sequitur
        if _cite_role(slug, cite, name) == "STANDARD":
            empty.append(f"{name or cite}, {cite} (standard-of-review authority; "
                         f"invoked, not distinguished)")
            continue
        holding = _holding_text(cite, name)
        if not holding:
            if _PULL_MISSING and _pull_missing_authority(cite, name):
                holding = _holding_text(cite, name)
        if not holding:
            empty.append(f"{name or cite}, {cite} (not in corpus)")
            continue
        prompt = (f"{_DISTINGUISH_SYS}\n\n"
                  f"AUTHORITY: {name}, {cite}\n\n"
                  f"HOLDING TEXT (official reporter):\n{holding}\n\n"
                  f"ESTABLISHED FACTS OF THIS RECORD:\n{facts}\n\n"
                  f"Write the factual distinction.")
        try:
            ans = (_call_direct(prompt, max_tokens=420, timeout=240) or "").strip()
        except Exception as e:
            empty.append(f"{name or cite}, {cite} (model error: {e})")
            continue
        if (not ans) or "NO DISTINCTION AVAILABLE" in ans.upper():
            empty.append(f"{name or cite}, {cite} (no distinction on this record)")
            continue
        ans = " ".join(ans.split())
        rows.append(f"- **{name}, {cite}.** {_strip_dangling_shortform(ans)}")
        rows.append("")
        made += 1
    if empty:
        # never silently drop: an authority with no distinction is stated as such
        rows.append(f"- The following authorities are answered on binding weight and "
                    f"scope rather than on a factual distinction: "
                    f"{'; '.join(empty)}.")
        rows.append("")
    print(f"  [{slug}] DISTINCTIONS: {made} drawn from corpus + record; "
          f"{len(empty)} answered on weight/scope")
    if made == 0:
        return block
    body = block or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        return "\n".join([lines[0], ""] + rows + lines[1:])
    return "\n".join(rows) + "\n" + body


def _fold_record_facts(slug: str, block: str) -> str:
    """Append the record facts the analysis argued WITHOUT.

    Rule 18 tells m0 to answer each objection with the dated document, the amount, the
    movant's own words — and the model wrote law only, three runs running. Same lesson as
    the spine: a prompt is a request, a fold is a guarantee. Deterministic, idempotent,
    and honest — the facts are quoted from the case's own evidence register, never invented.
    """
    base = CASES / slug / "drafts"
    if not base.is_dir() or "RECORD FACTS ANSWERING" in (block or ""):
        return block
    rows = []
    for p in sorted(base.rglob("*.md")):
        if "_DRAFT" in p.name or ".bak" in p.name:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        seg = re.search(r"^##\s*NEW EVIDENCE BLOCK(.*?)(?=^##\s|\Z)", t, re.S | re.M)
        if not seg:
            continue
        for m in re.finditer(
                r"^\s*[-*]\s*(E\d+)\s*[—\-–]\s*(.{4,90}?)\s*\|(.*?)(?=^\s*[-*]\s*E\d+|\Z)",
                seg.group(1), re.S | re.M):
            body = " ".join(m.group(3).split())
            fact = re.search(r"(?:fact|quote)\s*:\s*(.+?)(?:\s*\|\s*|$)", body, re.I)
            if fact:
                rows.append((m.group(1), m.group(2).strip(), fact.group(1).strip()))
        if rows:
            break
    if not rows:
        return block
    # only fold what the analysis actually omitted
    missing = [(tag, desc, f) for tag, desc, f in rows
               if not any(a and a in block for a in
                          re.findall(r"\b\d{2}/\d{2}/\d{4}\b|\$[\d,]+\.\d{2}", f))]
    if not missing:
        return block
    out = ["", "### RECORD FACTS ANSWERING THE OBJECTIONS "
           "(deterministically folded — the movant's own documents)", ""]
    for tag, desc, fact in missing[:10]:
        out.append(f"- **{desc}** — {fact} (Exhibit {tag}).")
    return (block.rstrip() + "\n" + "\n".join(out) + "\n")


def _upsert_block(cf_path: Path, header: str, block: str) -> bool:
    """Deterministically write `block` into CASE_FACTS.md: if a line equal to `header` exists, replace from
    it to the next top-level '## ' header (or EOF); else append. Existing curated sections are untouched.
    Returns True if the file now contains the header (i.e. the block landed)."""
    block = block.rstrip() + "\n"
    old = cf_path.read_text(encoding="utf-8") if cf_path.exists() else ""
    lines = old.splitlines(keepends=True)
    _h = header.strip()
    # match the exact header OR a variable-suffixed variant ("## LATEST COURT ORDER — Dkt 141 (May 11, 2026)")
    # so a re-centering task REPLACES a stale-suffixed section in place instead of appending a duplicate.
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == _h or ln.strip().startswith(_h + " —") or ln.strip().startswith(_h + " (")), None)
    if start is not None:
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        new = "".join(lines[:start]) + block + "\n" + "".join(lines[end:])
    else:
        sep = "" if (not old or old.endswith("\n\n")) else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block
    cf_path.write_text(new, encoding="utf-8")
    return header.strip() in cf_path.read_text(encoding="utf-8")


# Direct-completion path: the agentic Hermes skill-loop TIMES OUT on these sections (too many tool
# calls > SECTION_TIMEOUT). CASE_FACTS is ALREADY corpus-verified, so a section only needs to REORGANIZE
# grounded material into its block -- one fast :8000 chat completion (like m0), not an agentic loop.
_LLAMA_URL = os.environ.get("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
# default = the local-gpu LEGAL lane (user-locked). The old :8000 default silently
# pointed at a dead port whenever a caller forgot the env (2026-08-31 night run).


_SERVED_MODEL = None


def _resolve_model() -> str:
    """Return a model name the lane ACTUALLY serves.

    A stale MODEL default is indistinguishable, at the CASE_FACTS layer, from
    "the analyst found nothing": posting an unserved name returns HTTP 404
    ("The model 'X' does not exist"), every section fails, zero blocks persist,
    and the operator reads that as empty analysis. Cost of this exact failure,
    2026-09-26: MODEL defaulted to 'local-model-heretic' while :18020 served
    'qwen3.8-27b', all 8 sections 404'd, and a whole session was spent
    diagnosing retrieval and prompts that were never broken.

    So: ask the endpoint what it serves. Honor MODEL when the lane really has
    it; otherwise fall back to what is served and SAY SO loudly.
    """
    global _SERVED_MODEL
    if _SERVED_MODEL is not None:
        return _SERVED_MODEL
    import json as _json, urllib.request
    served = []
    try:
        base = _LLAMA_URL.split("/v1/")[0]
        with urllib.request.urlopen(f"{base}/v1/models", timeout=20) as r:
            served = [m.get("id") for m in (_json.loads(
                r.read().decode("utf-8")).get("data") or []) if m.get("id")]
    except Exception as e:
        print(f"  [model] WARNING: cannot list models at {_LLAMA_URL}: "
              f"{type(e).__name__}: {str(e)[:70]} — using configured {MODEL!r}")
        _SERVED_MODEL = MODEL
        return _SERVED_MODEL
    if MODEL in served:
        _SERVED_MODEL = MODEL
    elif served:
        _SERVED_MODEL = served[0]
        print(f"  [model] configured {MODEL!r} is NOT served by this lane "
              f"(serves {served}); using {_SERVED_MODEL!r}. Set BEEZLE_CF_MODEL "
              f"to silence this.")
    else:
        _SERVED_MODEL = MODEL
    return _SERVED_MODEL


def _call_direct(prompt: str, max_tokens: int = 4000, timeout: int = 300,
                 _retry: bool = False) -> str:
    import json as _json, urllib.request
    body = _json.dumps({
        "model": _resolve_model(),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.3, "top_p": 0.8,
        # Disable the model's chain-of-thought so it returns ONLY the finished block, not "thinking out
        # loud" (local-model leaks reasoning as markdown without <think> tags otherwise).
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(_LLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = _json.loads(resp.read().decode("utf-8"))
    ch = (d.get("choices") or [{}])[0]
    text = ch.get("message", {}).get("content", "") or ""
    if ch.get("finish_reason") == "length":
        text = _whole_sentences(text)
    if text.strip():
        return text
    # THINKING-BUDGET GUARD: a reasoning model can spend the whole completion budget
    # 'thinking' and return empty content (finish_reason 'stop', content None/"") — or be
    # length-capped mid-sentence so _whole_sentences returns "". The 2026-10-06
    # reply-brief run lost its premise-audit lines this way at max_tokens 1800-4000:
    # the section guard saw "" and CASE_FACTS landed without the audit. One retry at a
    # bigger budget with an explicit low-reasoning demand; a second empty answer is
    # honest (returns ""), not an infinite loop.
    if not _retry and max_tokens < 14000:
        print(f"  [m0] empty content at max_tokens={max_tokens} — retrying at 14000 "
              f"with explicit low-reasoning demand")
        return _call_direct(
            prompt + "\n\nAnswer directly in the final form only — no preamble, no "
                     "chain of thought, no working shown.",
            max_tokens=14000, timeout=max(timeout, 1200), _retry=True)
    return text


def _whole_sentences(text: str) -> str:
    """Drop a trailing incomplete sentence from a length-capped answer.

    The cut is on the last terminator that closes a sentence (not an ellipsis, not an
    abbreviation like "Pa." / "Co." / "v." followed by more text). If no terminator exists
    the answer is one unfinished sentence and nothing pleadable survives -> "".
    """
    t = (text or "").rstrip()
    if not t:
        return t
    if re.search(r"[.!?][\"\u201d')\]]*$", t) and not t.endswith("..."):
        return t
    best = -1
    for m in re.finditer(r"[.!?][\"\u201d')\]]*(?=\s)", t):
        pre = t[max(0, m.start() - 6): m.start() + 1]
        if pre.endswith("...") or re.search(
                r"\b(?:Pa|Co|Inc|Corp|v|vs|Cir|Super|Cmwlth|No|Nos|Ltd|Mr|Mrs|Dr|St|Assn|Ins|Cas|Mut|U\.S)\.$",
                pre):
            continue
        best = m.end()
    return t[:best].rstrip() if best > 0 else ""




def _evidence_probes(slug: str, max_per_file: int = 6) -> list:
    """EVIDENCE_ASSERT_V1: distinctive tokens (conf numbers, $amounts, dates) from intake
    evidence NEWER than CASE_FACTS.md — the facts a refresh MUST absorb. Deterministic."""
    base = CASES / slug
    cf = base / "CASE_FACTS.md"
    cf_mtime = cf.stat().st_mtime if cf.exists() else 0
    intake = base / "intake"
    probes = []
    if not intake.is_dir():
        return probes
    for f in sorted(intake.rglob("*.txt"), key=lambda x: x.stat().st_mtime):
        # ALL evidence sidecars participate — mtime is NOT the staleness signal, because a
        # refresh that failed to absorb still bumps CASE_FACTS' mtime. The absorption CHECK
        # (token present or not) is the decider. Docket sidecars are excluded (judicial
        # cases' orders aren't "evidence to absorb"; their lane is latest_order).
        if f.name == "index.json" or f.name.endswith((".doctext.txt", ".ocr.txt")):
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        toks, seen = [], set()
        for pat in (r"\$[\d,]+\.\d{2}", r"\b\d{6,}\b", r"\b\d{2}/\d{2}/\d{4}\b"):
            for m in re.findall(pat, txt):
                key = m.replace(",", "").lstrip("$")
                if key not in seen and len(toks) < max_per_file:
                    seen.add(key); toks.append(m)
        for t in toks:
            probes.append((f.name, t))
    return probes


def _assert_evidence_absorbed(slug: str, probes: list) -> list:
    """Which probe tokens are still missing from CASE_FACTS? Normalized compare."""
    cf = CASES / slug / "CASE_FACTS.md"
    if not cf.exists() or not probes:
        return probes
    body = cf.read_text(encoding="utf-8", errors="ignore").replace(",", "")
    return [(f, t) for (f, t) in probes if t.replace(",", "").lstrip("$") not in body]


# A party's motion/objections/brief packet — not a court order. These carry their
# ARGUMENT and AUTHORITIES in the back half, so they get a far larger context budget.
_ADVERSE_DOC = re.compile(
    r"BRIEF IN SUPPORT|PRELIMINARY OBJECTIONS|MOTION TO (?:DISMISS|STRIKE|COMPEL)"
    r"|MEMORANDUM OF LAW|Respectfully submitted|Counsel for (?:Defendant|Plaintiff)",
    re.I)


_FRONTMATTER = re.compile(r"\A\s*(?:---\s*\n)?((?:[A-Za-z_][\w\-]*:[^\n]*\n(?:[ \t]+-[^\n]*\n|[ \t]+[^\n]*\n)*)+)(?:---\s*\n)?")


def _strip_frontmatter(text: str) -> str:
    """Drop a scan-pipeline YAML header (message_id, dedup_hash, migrated_at, tags...).

    Only the leading key: value block is removed -- the body, including any "Date:" line
    that belongs to the document itself, is untouched. Text without a header is returned
    byte-identical.
    """
    t = text or ""
    m = _FRONTMATTER.match(t)
    if not m:
        return t
    head = m.group(1)
    # a real header has several metadata keys; a body that merely opens "Re: ..." does not
    if len(re.findall(r"(?m)^[A-Za-z_][\w\-]*:", head)) < 3:
        return t
    return t[m.end():].lstrip("\n")


def _section_context(slug: str, limit: int = 85000) -> str:
    """Assemble the grounded record a section reasons over: current CASE_FACTS (already corpus-verified)
    + the pinned timeline + the newest OCR'd intake filing(s). Bounded so one completion fits the window."""
    base = CASES / slug
    intake = base / "intake"
    # 1. OPERATIVE ORDER FIRST — surfaced with guaranteed budget so it is NEVER truncated. CASE_FACTS alone
    #    can exceed `limit`, and when the intake was appended AFTER it, the newest order got cut entirely and
    #    the model reasoned off the stale CASE_FACTS. Pick the HIGHEST-numbered docket doc + the NEWEST by mtime.
    order_parts = []
    if intake.is_dir():
        primary = list(intake.glob("*.doctext.txt")) + list(intake.glob("*.ocr.txt"))
        if primary:
            def _dktnum(p):
                m = re.search(r"dkt[_ ]?(\d+)", p.name, re.I)
                return int(m.group(1)) if m else -1
            by_dkt = sorted([p for p in primary if _dktnum(p) >= 0], key=_dktnum)
            by_mtime = sorted(primary, key=lambda p: p.stat().st_mtime)
            picks = []
            for p in (by_dkt[-2:] + by_mtime[-2:]):
                if p not in picks:
                    picks.append(p)
        else:
            picks = [p for p in sorted(intake.rglob("*.txt")) if p.name != "index.json"][-4:]
        for f in picks:
            _raw = f.read_text(encoding="utf-8", errors="ignore")
            # PER-DOCUMENT BUDGET (fixed 2026-09-23). The old flat [:8000] was sized for
            # COURT ORDERS, which are short. An adversary's motion-plus-brief packet runs
            # 15K+ chars and its ARGUMENT section — every authority it relies on — sits in
            # the BACK half. Truncating at 8000 silently deleted all five federal citations
            # from a preliminary-objections packet, and the analysis then reported, truthfully
            # from where it stood, "no federal authority is cited." A document's authorities
            # are the part an adverse-filing analysis exists to examine; never cut them.
            _budget = 8000
            if _ADVERSE_DOC.search(_raw[:4000]):
                _budget = 30000
            _txt = _raw[:_budget]
            if len(_raw) > _budget:
                # make truncation VISIBLE — a silent cut reads as absence to the model
                _txt += (f"\n\n[... TRUNCATED: {len(_raw) - _budget} of {len(_raw)} characters "
                         f"omitted. Any authority or argument not shown above was NOT read — "
                         f"do not conclude it is absent from the filing.]")
            order_parts.append(f"=== NEWEST / OPERATIVE FILING — intake/{f.relative_to(intake)} "
                               f"(anchor the analysis HERE, not to a prior document) ===\n" + _txt)
    orders = "\n\n".join(order_parts)
    # 1c. ORGAN OUTPUT (2026-09-23) — m0 reasons over the adverse filing but had no
    # access to the organs that already hold the answers, so findings the machine can
    # compute were being re-derived by hand: the enacted text and controlling cases sit
    # corpus-verbatim in m0_authority_spine, the attacked-vs-spared remedy arithmetic in
    # remedy_strip_detector, and the real holdings in the opinions the corpus already
    # carries. Nothing here asserts a finding — each organ derives its own output from
    # THIS case's claim taxonomy / THIS filing's citations, so it stays agnostic.
    organ_parts = []
    try:
        _tax_terms = []
        _cf = base / "CASE_FACTS.md"
        if _cf.exists():
            _t = _cf.read_text(encoding="utf-8", errors="ignore")
            _m = re.search(r"^##\s*CAUSES OF ACTION AND ELEMENTS(.*?)(?=^## )", _t, re.S | re.M)
            if _m:
                _tax_terms += re.findall(r"^###\s*\d*\.?\s*(.+?)\s*$", _m.group(1), re.M)
        # CASE_FACTS can be partial (a truncated or rejected run leaves only some counts),
        # and the counts a filing ATTACKS are exactly the ones needing authority. Derive the
        # claim vocabulary from the adverse filing itself too, so the spine is asked about
        # every claim actually in play. Agnostic: terms come from the documents, not a list.
        for _f in (picks if intake.is_dir() else []):
            _ft = _f.read_text(encoding="utf-8", errors="ignore")
            for _cm in re.finditer(
                    r"\b(unjust enrichment|conversion|breach of contract|bad faith|"
                    r"unfair trade practices(?:\s+and\s+consumer protection law)?|UTPCPL|"
                    r"fraud|negligence|civil conspiracy|"
                    r"breach of (?:fiduciary duty|the implied covenant of good faith[^,.\n]{0,20})|"
                    r"constructive trust|quiet title|declaratory judgment|"
                    r"replevin|trespass|defamation|"
                    r"gist of the action)\b", _ft, re.I):
                _tax_terms.append(_cm.group(1))
            # statutes the filing argues about are claim anchors too (agnostic shapes)
            for _sm in re.finditer(
                    r"\b\d{1,2}\s+(?:P\.S\.|Pa\.C\.S\.|U\.S\.C\.)\s*§*\s*[\d.\-]+", _ft):
                _tax_terms.append(_sm.group(0))
        # case-insensitive dedupe — "BAD FAITH"/"Bad Faith"/"bad faith" are one claim, and
        # duplicates push later claims past the budget (that is how the enacted text for a
        # claim listed LAST kept vanishing).
        _seen, _uniq = set(), []
        for _x in (x.strip() for x in _tax_terms if x.strip()):
            if _x.lower() not in _seen:
                _seen.add(_x.lower()); _uniq.append(_x)
        _tax = ", ".join(_uniq)
        if _tax:
            from pipeline.m0_authority_spine import build_section as _spine
            _sec, _rep = _spine(_tax)
            organ_parts.append(
                "=== CONTROLLING AUTHORITY (m0_authority_spine — corpus-verbatim, ladder order: "
                "ENACTED TEXT governs, decisions construe it). Quote these directly; do not "
                "paraphrase or re-derive them. ===\n" + _sec[:26000])
    except Exception as _e:
        organ_parts.append(f"[spine unavailable: {str(_e)[:80]}]")
    try:
        from pipeline.remedy_strip_detector import detect as _rs
        _pl = ""
        for _p in sorted(intake.glob("*COMPLAINT*.txt")) + sorted(intake.glob("*complaint*.txt")):
            _pl = _p.read_text(encoding="utf-8", errors="ignore"); break
        for _f in (picks if intake.is_dir() else []):
            _raw2 = _f.read_text(encoding="utf-8", errors="ignore")
            for _hit in _rs(_raw2, _pl or _raw2, _f.name):
                organ_parts.append(
                    "=== DETERMINISTIC FINDING (remedy_strip_detector) ===\n"
                    f"{_hit['violation_type']}: {_hit['explanation']}\n"
                    f"RULE: {_hit['rule_citation']}\nUSE: {_hit['fix']}")
    except Exception:
        pass
    try:
        _cites = set()
        for _f in (picks if intake.is_dir() else []):
            _txt2 = _f.read_text(encoding="utf-8", errors="ignore")
            for _c in re.finditer(r"\b(\d{1,3}\s+[A-Z][A-Za-z.]*\.?\s?(?:2d|3d|4th)?\s+\d{1,4})\b", _txt2):
                _cites.add(_c.group(1))
        if _cites:
            import sqlite3 as _sq
            # CHUNK_DB is not defined in this module — the corpus path lives in
            # turbovec_ingest. A bare `except: pass` swallowed the NameError and the
            # opinion block silently never built.
            try:
                from pipeline.turbovec_ingest import CHUNK_DB as _CDB
            except Exception:
                _CDB = str(_eroot() / "corpus/chunk_text.db")
            _con = _sq.connect(f"file:{_CDB}?mode=ro", uri=True, timeout=30)
            _op = []
            for _c in sorted(_cites)[:8]:
                _r = _con.execute(
                    "SELECT source, substr(text,1,1200) FROM chunk_text "
                    "WHERE citations LIKE ? OR text LIKE ? LIMIT 1",
                    (f"%{_c}%", f"%{_c}%")).fetchone()
                if _r:
                    _op.append(f"--- {_c} [corpus: {_r[0]}] ---\n{' '.join(_r[1].split())}")
            if _op:
                organ_parts.append(
                    "=== OPINION TEXT for authorities the adverse filing cites (corpus). "
                    "Check what each case ACTUALLY held against what the filing says it held; "
                    "a cite whose opinion is not shown here is NOT-VERIFIED, not absent. ===\n"
                    + "\n\n".join(_op)[:12000])
            _con.close()
    except Exception:
        pass
    # order by value-per-char: deterministic findings and opinion text are short and
    # decisive; the spine is long. A budget cut must never eat the short ones.
    organ_parts.sort(key=lambda b: 0 if "DETERMINISTIC FINDING" in b[:80]
                     else (1 if "OPINION TEXT" in b[:80] else 2))
    organs = "\n\n".join(organ_parts)
    # 1b. EVIDENCE CORPUS (EVIDENCE_FOLD_V1, 2026-08-31): ALL intake evidence newer than
    # CASE_FACTS itself — pre-filing cases have no "operative order"; their record IS the
    # evidence set (bank/email/scan sidecars in intake/ and intake/_ocr/). Without this
    # fold the refresh reasons off stale CASE_FACTS while fresh verified evidence sits
    # unread (root cause of the Progressive stale-claims miss, 2026-08-30).
    cf_p = base / "CASE_FACTS.md"
    cf_mtime = cf_p.stat().st_mtime if cf_p.exists() else 0
    seen = {str(f) for f in (picks if "picks" in dir() else [])}
    ev_parts = []
    if intake.is_dir():
        ev_files = [f for f in sorted(intake.rglob("*.txt"), key=lambda x: x.stat().st_mtime)
                    if f.name != "index.json" and str(f) not in seen
                    and not f.name.endswith((".doctext.txt", ".ocr.txt"))]
        newest_first = list(reversed(ev_files))
        budget = 18000
        for f in newest_first:
            if budget <= 0:
                break
            txt = _strip_frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
            take = min(len(txt), 3000, budget)
            fresh = " (NEWER THAN CASE_FACTS — its facts OVERRIDE any conflicting CASE_FACTS text)" \
                    if f.stat().st_mtime > cf_mtime else ""
            ev_parts.append(f"=== EVIDENCE — intake/{f.relative_to(intake)}{fresh} ===\n" + txt[:take])
            budget -= take
    if ev_parts:
        orders = orders + "\n\n" + "\n\n".join(ev_parts) if orders else "\n\n".join(ev_parts)
    # 1c. ANALYSIS SIDECARS (INITIAL_CLAIM_V1, 2026-08-31): scorecard*.md /
    # audit*.md in the case ROOT carry claim-defining findings that never made
    # CASE_FACTS (4767: 'Broken chain of title in mortgage assignment' lived
    # ONLY in scorecard_20260516.md). Fold them so aff_initial can see them.
    sc_parts = []
    for f in sorted(base.glob("*.md")):
        if f.name.startswith(("scorecard", "audit", "AIRTIGHT")):
            txt = f.read_text(encoding="utf-8", errors="ignore")
            sc_parts.append(f"=== ANALYSIS SIDECAR — {f.name} (claim-defining findings; "
                            f"fold into INITIAL CLAIM) ===\n" + txt[:5000])
    if sc_parts:
        orders = orders + "\n\n" + "\n\n".join(sc_parts[:3]) if orders else "\n\n".join(sc_parts[:3])
    # 2. timeline (small)
    tl = base / "CASE_TIMELINE.json"
    tl_text = tl.read_text(encoding="utf-8", errors="ignore")[:6000] if tl.exists() else ""
    # 3. CASE_FACTS truncated to whatever budget remains AFTER the operative order + timeline
    # ORGAN OUTPUT rides with the filing (guaranteed budget) — it is the verified material
    # the analysis must quote rather than re-derive.
    # ORGAN BUDGET IS RESERVED. A long adverse filing must not squeeze out the verified
    # authority the analysis is required to quote — that is exactly how the enacted text
    # went missing while the model had the full brief in front of it.
    _organ_budget = min(len(organs), 34000) if organs else 0
    if _organ_budget and len(orders) > (limit - _organ_budget - len(tl_text) - 2000):
        orders = orders[:max(0, limit - _organ_budget - len(tl_text) - 2000)]
    out = orders
    if organs:
        out += "\n\n" + organs[:_organ_budget]
    cf = base / "CASE_FACTS.md"
    if cf.exists():
        remaining = max(0, limit - len(out) - len(tl_text) - 400)
        out += ("\n\n=== CURRENT CASE_FACTS.md (corpus-verified; preserve its facts — but the ORDER above is "
                "the operative disposition and controls the posture) ===\n"
                + cf.read_text(encoding="utf-8", errors="ignore")[:remaining])
    if tl_text:
        out += "\n\n=== CASE_TIMELINE.json (pinned dockets) ===\n" + tl_text
    return out[:limit]


_CA_HEADER = "## CONTROLLING AUTHORITIES (corpus-verified — verbatim; the drafter quotes these, no live retrieval)"


def _write_controlling_authorities(slug: str, cf_path) -> int:
    """L4 (holding retrieval at ANALYSIS time, not draft time): pre-compute the VERBATIM text of the case's
    controlling authorities from the corpus ONCE, into a CASE_FACTS section the drafter already reads — so the
    drafter never runs slow live corpus retrieval per section. Cites are gathered from CASE_FACTS + the audit's
    detected procedural_violations; verbatim text comes from the fast family-keyed rules_index. Never raises."""
    try:
        import json as _json, re as _re
        cf = cf_path.read_text(encoding="utf-8", errors="ignore") if cf_path.exists() else ""
        cites = set(_re.findall(
            r"Pa\.?\s?R\.?[A-Za-z.]*\s?\d[\w().-]*|Fed\.?\s?R\.?[A-Za-z.]*\s?\d[\w().-]*|"
            r"\d+\s+U\.?\s?S\.?\s?C\.?\s?§?\s?\d[\w().-]*|\d+\s+C\.?F\.?R\.?\s?§?\s?[\d.]+|"
            r"\d+\s+Pa\.?\s?C\.?\s?S\.?\s?§?\s?\d[\w().-]*", cf))
        digits = "".join(c for c in slug if c.isdigit())
        for f in (ROOT / "output" / "audit").glob(f"data_*{digits}*.json"):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
                for v in (d.get("procedural_violations") or []):
                    c = (v.get("rule_citation") or "").strip()
                    if c:
                        cites.add(c)
            except Exception:
                pass
        if not cites:
            return 0
        from pipeline import rules_index as _ri
        lines, n = [_CA_HEADER + "\n"], 0
        for c in sorted(cites)[:40]:
            try:
                r = _ri.lookup(c, limit=1)
            except Exception:
                r = None
            body = ((r[0].get("body") if (r and isinstance(r, list)) else "") or "")
            if body.strip():
                lines.append(f"- **{c}** — {' '.join(body.split())[:400]}")
                n += 1
        if n:
            _upsert_block(cf_path, _CA_HEADER, "\n".join(lines))
        return n
    except Exception:
        return 0


_APPEAL_HEADER = "## THIRD CIRCUIT APPEAL"   # matches the existing section so upsert REPLACES it in place


def _write_appeal_status(slug: str, cf_path) -> int:
    """V1: derive the CURRENT circuit appeal from docket_events.md (the notice-only NEF ledger — the grounded
    source), not stale hand-written CASE_FACTS. Emits the live USCA appeal (newest) with its FRAP-31 trigger
    and who the appellant is, and lists any OTHER appeal number found as a PRIOR/superseded appeal — so a
    dismissed prior (e.g. 25-2960) is retired, never shown as active. Data-driven (R2). Never raises."""
    try:
        dv = (CASES / slug / "docket_events.md")
        if not dv.exists():
            return 0
        text = dv.read_text(encoding="utf-8", errors="ignore")
        # each appeal-trigger NEF: date | ... | USCA <num> | ... | note(FRAP/appellant)
        appeals = []   # (num, date, note)
        for ln in text.splitlines():
            mnum = re.search(r"USCA\s+(\d{2}-\d{3,5})", ln)
            if not mnum or "APPEAL" not in ln.upper():
                continue
            mdate = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", ln)
            frap = re.search(r"(FRAP\s*\d+[^|]*)", ln)
            appel = re.search(r"appellant:\s*([^|]+)", ln, re.I)
            note = " — ".join([x.group(1).strip().rstrip(") ") for x in (frap, appel) if x])
            appeals.append((mnum.group(1), mdate.group(1) if mdate else "", note))
        if not appeals:
            return 0
        # newest by date = the LIVE appeal; distinct earlier numbers = prior/superseded
        appeals.sort(key=lambda a: a[1] or "0000-00-00")
        live_num, live_date, live_note = appeals[-1]
        priors = sorted({a[0] for a in appeals if a[0] != live_num})
        # also treat any DIFFERENT appeal number already named in CASE_FACTS as a prior (so 25-2960 retires)
        cf = cf_path.read_text(encoding="utf-8", errors="ignore") if cf_path.exists() else ""
        for m in re.finditer(r"\b(?:C\.A\.\s*No\.\s*|USCA\s*)(\d{2}-\d{3,5})", cf):
            if m.group(1) != live_num and m.group(1) not in priors:
                priors.append(m.group(1))
        lines = [_APPEAL_HEADER,
                 "*(docket-derived from docket_events.md — the live appeal + its FRAP clock)*\n",
                 f"- **Live appeal**: USCA No. {live_num}"
                 + (f" (docketed {live_date})" if live_date else ""),
                 f"- **Status**: active — {live_note}" if live_note else "- **Status**: active"]
        if priors:
            lines.append(f"- **Prior appeal(s)**: {', '.join('No. ' + p for p in priors)} — "
                         f"superseded/dismissed; NOT the operative appeal (do not brief).")
        _upsert_block(cf_path, _APPEAL_HEADER, "\n".join(lines))
        # retire the stale hand-written '## THIRD CIRCUIT APPEAL' block if it lingers separately
        return 1
    except Exception:
        return 0


def _slug_to_case_id_cfr(slug: str):
    """Map a case folder slug back to its canonical case_id (grounding_cache keys by case_id, which the drafter
    reads from the stub). Uses the registry; falls back to None so baking is simply skipped, never wrong."""
    try:
        import json as _json
        reg = _json.loads((ROOT / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
        cases = reg if isinstance(reg, list) else (reg.get("cases") or list(reg.values()))
        for c in (cases if isinstance(cases, list) else []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("canonical_id") or "")
            if not cid:
                continue
            s = c.get("slug") or ("fed_" + cid.split(":")[-1] if cid.startswith("fed") else cid.replace(":", "_"))
            if s == slug:
                return cid
    except Exception:
        pass
    return None


def _bake_drafter_grounding(slug: str, cf_path) -> int:
    """B (draft-speed): pre-compute the drafter's CORPUS-RETRIEVAL grounding (canon/restoration/pseudolaw/
    definition) at ANALYSIS time from a case-level seed (CASE_FACTS + audit violations) and cache it, so the
    drafter reads it instead of running the ~2-min corpus retrieval at draft time. Non-breaking: if nothing is
    baked, the drafter retrieves live. Never raises."""
    try:
        import os as _os, json as _json
        from pipeline import grounding_cache as _gc
        cid = _slug_to_case_id_cfr(slug)
        if not cid:
            return 0
        seed = cf_path.read_text(encoding="utf-8", errors="ignore") if cf_path.exists() else ""
        digits = "".join(c for c in slug if c.isdigit())
        for f in (ROOT / "output" / "audit").glob(f"data_*{digits}*.json"):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
                for v in (d.get("procedural_violations") or []):
                    seed += "\n" + " ".join(str(v.get(k, "")) for k in
                                            ("violation_type", "rule_citation", "description", "rule_text"))
                for v in ((d.get("scorecard") or {}).get("violations") or []):
                    seed += "\n" + " ".join(str(v.get(k, "")) for k in ("category", "description"))
            except Exception:
                pass
        _os.environ["BEEZLE_GROUNDING_BAKING"] = "1"   # force live compute (don't read a stale cache while baking)
        try:
            return _gc.bake(cid, seed[:40000])
        finally:
            _os.environ.pop("BEEZLE_GROUNDING_BAKING", None)
    except Exception:
        return 0




_PERIOD_RE = re.compile(
    r"(?P<subj>[A-Za-z][\w' ]{4,60}?)\s+(?:paid|retained|withheld|owed|accru\w+|"
    r"collected|charged)?\s*from\s+(?P<d1>(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{1,2},\s+\d{4}|\d{2}/\d{2}/\d{4})"
    r"(?:\s*,?\s*(?:through|to|until)\s+(?P<d2>(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}|"
    r"\d{2}/\d{2}/\d{4}|the date of [a-z]+))?", re.I)

_PERIOD_STOP = {"the", "a", "an", "of", "in", "amount", "total", "plaintiff", "defendant",
                "his", "her", "their", "and", "damages", "suffered", "entitled", "is",
                "was", "were", "constitute", "record", "establishes", "that"}


def _period_claims(text: str) -> list:
    """Every 'X from DATE [through DATE]' claim in the document, keyed by subject."""
    out = []
    for m in _PERIOD_RE.finditer(text or ""):
        toks = [w.lower().strip("*_-—:;,.") for w in m.group("subj").split()]
        key = frozenset(w for w in toks if w and w not in _PERIOD_STOP and len(w) > 3)
        if not key:
            continue
        line_start = (text or "").rfind("\n", 0, m.start()) + 1
        line_end = (text or "").find("\n", m.end())
        out.append({
            "key": key,
            "start": _norm_date(m.group("d1")) or m.group("d1"),
            "start_raw": m.group("d1"),
            "end_raw": m.group("d2") or "",
            "line": " ".join((text or "")[line_start:line_end if line_end > 0 else None].split()),
        })
    return out



def _current_block(doc: str, header: str) -> str:
    """The existing text of a '## HEADER' block, or '' when absent."""
    if not doc or not header:
        return ""
    i = doc.find(header)
    if i < 0:
        return ""
    j = doc.find("\n## ", i + len(header))
    return doc[i:j if j > 0 else len(doc)]


_QUANTITY_HEADS = {"premium", "premiums", "payment", "payments", "interest",
                   "refund", "refunds", "charge", "charges", "fee", "fees",
                   "balance", "damages", "principal"}


def _audit_period_grounding(slug: str, text: str) -> list:
    """Flag a period whose START DATE the SOURCE RECORD never ties to that quantity.

    Internal consistency is not correctness: if every section repeats the same wrong
    date, the cross-section audit sees no conflict. This checks the analysis against the
    INTAKE EVIDENCE instead of against itself — the only thing that can tell the
    difference between 'agreed' and 'right'.

    Class-level: a date used to bound a quantity must appear somewhere in the source
    record in the same breath as that quantity. No case names, no date literals.
    """
    src = []
    ocr = CASES / slug / "intake" / "_ocr"
    if ocr.exists():
        for f in sorted(ocr.glob("*.txt")):
            try:
                src.append(f.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
    if not src:
        return []
    blob = "\n".join(src)
    low = blob.lower()
    # Learn date roles from the ENTIRE record (source evidence + the case file), never
    # from the fragment under audit -- a single sentence carries almost no role evidence.
    _cf_path = CASES / slug / "CASE_FACTS.md"
    _cf_text = ""
    if _cf_path.exists():
        try:
            _cf_text = _cf_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            _cf_text = ""
    _record_roles = _date_roles_from_record(blob + "\n" + _cf_text + "\n" + (text or ""))
    problems, seen = [], set()
    for c in _period_claims(text):
        d_raw = c["start_raw"]
        iso = _norm_date(d_raw) or ""
        if not iso:
            continue
        y, mo, dy = iso.split("-")
        months = ("January February March April May June July August September October "
                  "November December").split()
        forms = {d_raw, f"{mo}/{dy}/{y}", f"{int(mo)}/{int(dy)}/{y}",
                 f"{months[int(mo) - 1]} {int(dy)}, {y}"}
        # every place the source record mentions this date
        windows = []
        for form in forms:
            start = 0
            fl = form.lower()
            while True:
                i = low.find(fl, start)
                if i < 0:
                    break
                windows.append(low[max(0, i - 220):i + 220])
                start = i + 1
        if not windows:
            continue   # date absent from source entirely — a different audit's problem
        subject = sorted(c["key"])
        if not subject:
            continue
        # Only audit quantities the record can actually corroborate by name. A phrase
        # like "aggregate monthly payments" describes the SAME money as "unearned
        # premiums"; requiring both phrasings to appear verbatim in the source produces
        # false alarms. Anchor on the head noun the source would use.
        _heads = [w for w in subject if w in _QUANTITY_HEADS]
        if not _heads:
            continue
        subject = _heads
        # PROXIMITY IS NOT A RELATION. "premium" appearing within a few hundred
        # characters of a date does not make that date the start of a premium period --
        # every one of these emails mentions premiums. A period start must be a date the
        # RECORD ties to an accrual-starting EVENT (disposition of the property, the
        # cancellation, or the acquisition), not merely a date sharing a paragraph.
        _roles_all = _record_roles
        _admissible = {d for d, c2 in _roles_all.items()
                       if _dominant_roles(c2) & {"dispose", "cancel", "acquire"}}
        if iso in _admissible:
            continue
        near = any(any(w in win for w in subject) for win in windows)
        sig = (iso, tuple(subject))
        if sig in seen:
            continue
        seen.add(sig)
        if not near:
            problems.append(
                f"period start '{d_raw}' is never tied to '{' '.join(subject)}' anywhere "
                f"in the source record -- {c['line'][:110]}")
            continue
        _has_role = _dominant_roles(_roles_all.get(iso, {}))
        problems.append(
            f"period start '{d_raw}' is not an accrual-starting event in the record "
            f"(record role: {sorted(_has_role) or 'none'}; a period must start at the "
            f"disposition, the acquisition, or the cancellation) -- {c['line'][:110]}")
        continue
    return problems


def _audit_cross_section(text: str) -> list:
    """Flag the SAME quantity given two different periods in one document.

    A filing that says unearned premiums ran 'from February 1, 2026' in one section and
    'from March 21, 2023' in another states two different damages figures. Neither the
    per-section guards nor the evidence walk can see it: each sentence is locally fine.
    Structural and case-agnostic — it compares the document against itself.
    """
    claims = _period_claims(text)
    problems, seen = [], set()
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            shared = a["key"] & b["key"]
            if len(shared) < 2 or a["start"] == b["start"]:
                continue
            sig = tuple(sorted([a["start"], b["start"]])) + (tuple(sorted(shared)),)
            if sig in seen:
                continue
            seen.add(sig)
            problems.append(
                f"same quantity ({' '.join(sorted(shared))}) given two different start "
                f"dates: '{a['start_raw']}' vs '{b['start_raw']}' -- [1] {a['line'][:100]} "
                f"|| [2] {b['line'][:100]}")
    return problems


# F4 SWORN FACTS (card t_8fbf9767): if the case carries sworn facts (a verified response,
# an affidavit) and the movant's paper answers them with silence, that silence is itself a
# legal event — "AVERMENTS IN A PLEADING TO WHICH A RESPONSIVE PLEADING IS REQUIRED ARE
# ADMITTED WHEN NOT DENIED SPECIFICALLY OR BY NECESSARY IMPLICATION (Pa.R.C.P. 1029(b),
# verbatim from the corpus). The premise audit asks the model (Q10_sworn_facts); the case
# file must state the answer regardless, so the omission cannot again ride the model's
# attention (E5: 'the single most powerful lever isn't in the fold').
_SWORN_LANG = re.compile(r"\b(affidavit|verif\w+|I hereby|under penalty of perjury)\b", re.I)
_SWORN_ENGAGE = re.compile(r"\b(affidavit|1029|admit\w*|deemed|verif\w*)\b", re.I)
_RULE_1029_B = ("Averments in a pleading to which a responsive pleading is required are "
                "admitted when not denied specifically or by necessary implication "
                "(Pa.R.C.P. 1029(b), verbatim)")


def _sworn_instrument(slug: str, adverse_head: str = ""):
    """If the record shows sworn facts were filed, return (label, text) — else None.

    Our filed instruments live in record/ and drafts/ (intake/ holds THEIR papers and the
    acceptance notices). Prefer the verified pleading the current adverse paper actually
    ANSWERS: a reply answers our response in opposition; objections answer our complaint.
    The match is on the paper's CONTENT, not its filename — e-filing-notice wrappers carry
    court-system names that say nothing about what they contain. Otherwise prefer the
    FILING file over drafts so the label names the instrument actually before the court."""
    base = CASES / slug
    cands = []
    for d in ("record", "drafts"):
        dpath = base / d
        if not dpath.is_dir():
            continue
        cands += [p for p in list(dpath.rglob("*.md")) + list(dpath.rglob("*.txt"))
                  if ".bak" not in p.name]

    def _prio(p):
        n = p.name.lower()
        if "filing" in n and "draft" not in n:
            s = 0
        elif "filing" in n:
            s = 1
        elif "final" in n:
            s = 2
        elif "m1_draft" in n:
            s = 3
        else:
            s = 4
        # Relevance is decided by the adverse paper's CONTENT (its filename is a
        # court-system e-filing wrapper that says nothing). The sworn pleading that was
        # ANSWERED is the one whose facts 1029(b) deems admitted:
        #  - a reply to our response in opposition answers that response;
        #  - a preliminary objection (not a reply) answers our complaint.
        an = (adverse_head or "").lower()
        if "response in opposition" in an:
            if "response_in_opposition" in n:
                s = min(s, 0)
        elif "objection" in an:
            if "complaint" in n:
                s = min(s, 0)
        return s

    for p in sorted(cands, key=_prio):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(txt) < 300 or not _SWORN_LANG.search(txt):
            continue
        return p.name, txt
    return None


def _current_adverse_filing(slug: str):
    """The newest intake PARTY filing — the movant's paper the sections are currently
    reasoning over (same population _section_context picks: *.doctext.txt / *.ocr.txt;
    court orders and acceptance notices are filtered out — silence can only belong to a
    party's answer, never to the court's order)."""
    intake = CASES / slug / "intake"
    if not intake.is_dir():
        return None
    primary = list(intake.glob("*.doctext.txt")) + list(intake.glob("*.ocr.txt"))
    party = []
    for p in primary:
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        if _ADVERSE_DOC.search(head):
            party.append(p)
    if not party:
        return None
    return max(party, key=lambda p: p.stat().st_mtime)


def _sworn_facts_audit(slug: str, cf_path):
    """Return updated CASE_FACTS text if the SWORN FACTS UNANSWERED fold was added, else None."""
    doc = cf_path.read_text(encoding="utf-8", errors="ignore")
    if "SWORN FACTS UNANSWERED" in doc:
        return None                      # idempotent — a prior pass already wrote the fold
    f = _current_adverse_filing(slug)
    if not f:
        return None
    adverse = f.read_text(encoding="utf-8", errors="ignore")
    if not _ADVERSE_DOC.search(adverse[:4000]):
        return None                      # newest intake paper is an order/notice, not a party paper
    inst = _sworn_instrument(slug, adverse[:4000])
    if not inst:
        return None
    inst_name, _inst_text = inst
    if _SWORN_ENGAGE.search(adverse):
        return None                      # engaged (admit/deny/verify/1029) — Q10 grades HOW
    # The unanswered-unit list: the instrument's own numbered units. A sequence that
    # restarts means a sub-list (definitions, prayers), not the document's units — so
    # take the LONGEST consecutive run across all numbering styles: bold numbered lines
    # (per-PO response heads), numbered headings, or numbered paragraphs.
    def _longest_run(pattern):
        m = re.findall(pattern, _inst_text)
        if not m:
            return []
        best, cur = [m[0]], [m[0]]
        for x in m[1:]:
            if int(x) == int(cur[-1]) + 1:
                cur.append(x)
            else:
                if len(cur) > len(best):
                    best = cur
                cur = [x]
        if len(cur) > len(best):
            best = cur
        return best if len(best) >= 3 else []

    unit = max((_longest_run(p) for p in
                (r"(?m)^\*\*\s*(\d{1,3})[.)]\s",
                 r"(?m)^#{1,4}\s*(\d{1,3})[.)]\s",
                 r"(?m)^\s*(\d{1,3})\.\s+\S")),
               key=len)
    if unit:
        plist = ", ".join(unit[:20]) + (", …" if len(unit) > 20 else "")
    else:
        heads = re.findall(r"(?m)^#{2,4}\s+(.{5,80})$", _inst_text)
        plist = "; ".join(h.strip() for h in heads[:10]) or "the pleaded averments"
    block = (
        "\n### SWORN FACTS UNANSWERED (deterministic; card t_8fbf9767)\n"
        f"Our sworn instrument **{inst_name}** carries sworn averments, and the movant's "
        f"paper (intake/{f.name}) contains ZERO engagement with them (0 hits: "
        f"affidavit/verif/1029/admit/deemed). {_RULE_1029_B} — the paper is read as "
        f"answering sworn facts it chose not to answer; plead the admission. Unanswered "
        f"units: {plist}\n"
    )
    lines = doc.splitlines(keepends=True)
    hdr = _SECTION_HEADERS["rebuttal"]
    out, inserted, i = [], False, 0
    while i < len(lines):
        ln = lines[i]
        if not inserted and ln.startswith(hdr):
            out.append(ln)
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("#"):
                out.append(lines[i])
                i += 1
            out.append(block)
            inserted = True
            continue
        out.append(ln)
        i += 1
    if not inserted:
        out.append(block)
    return "".join(out)


# F5 SLOT-FILL GUARD (card t_8fbf9767): every docket number written into the case file
# must exist in the case's OWN record, tied to the instrument the sentence says it
# belongs to. The 2026-10-06 reply-brief intake drifted two ways: the REPLY was stamped
# with our response's e-file no. (2601000000 — nearest number, wrong instrument) and
# with the POs' CONTROL no. cited as an e-file no. (26010000). Prose guidance
# ("prefer blank over wrong", E4) is not a guardrail; the record is. A number found
# nowhere in the record -> "(not in record)"; a real number paired with a different
# instrument -> that instrument's own authoritative number; a control no. cited as an
# e-file no. -> relabeled (the number is real, blanking it loses information).
_F5_INSTRUMENTS = (
    ("sur_reply", re.compile(r"sur[- ]?reply", re.I)),
    ("leave", re.compile(r"motion for leave", re.I)),
    ("cos", re.compile(r"certificate of service|affidavit of service|\bcos\b", re.I)),
    ("complaint", re.compile(r"\bcomplaint\b", re.I)),
    ("reply", re.compile(r"\breply\b|reply[-_ ]brief", re.I)),
    ("response", re.compile(r"opposition", re.I)),
    ("po", re.compile(r"preliminary objection", re.I)),
)
_F5_DATE = (r"(?:January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2}")


def _f5_instrument(text: str):
    """The instrument class a fragment names — first match in priority order
    (sur-reply before reply; COS before complaint — 'Certificate of Service, Complaint'
    is ONE paper, and so is the leave motion that carries a sur-reply as its exhibit)."""
    for name, rx in _F5_INSTRUMENTS:
        if rx.search(text or ""):
            return name
    return None


def _f5_all_instruments(text: str):
    return {name for name, rx in _F5_INSTRUMENTS if rx.search(text or "")}


def _f5_iso(raw: str):
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw or "")
    if m:
        return raw
    m = re.fullmatch(r"(" + r"|" .join(
        ["January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"]) + r")\s+(\d{1,2}),\s+(\d{4})",
        raw or "")
    if not m:
        return None
    mon = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"].index(m.group(1)) + 1
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


def _f5_registry(slug: str):
    """Authoritative docket data for the case, from the record ONLY:
    numbers: {number: {'labels': set, 'instruments': set}}
    efiles:  {instrument: set(e-file numbers)}
    dates:   {instrument: 'YYYY-MM-DD'} — one deemed-filed date per instrument only;
        when the record claims two, the date fix stands down for that instrument
        (an ambiguous record must not drive a "fix").
    Sources: CASE_TIMELINE.json docket_refs/notes, intake+record filenames, frontmatter,
    labeled body lines ('E-File No.:' / 'Motion Control No.:' / 'Date Deemed Filed'),
    and instrument tables (| paper | e-file | deemed filed | accepted |)."""
    base = CASES / slug
    numbers, efiles, date_claims = {}, {}, {}

    def _add(num, label, instruments=()):
        if not re.fullmatch(r"\d{6,10}", num or ""):
            return
        d = numbers.setdefault(num, {"labels": set(), "instruments": set()})
        d["labels"].add(label)
        for i in instruments:
            d["instruments"].add(i)
            if label == "efile":
                efiles.setdefault(i, set()).add(num)

    def _claim(instruments, raw):
        iso = _f5_iso(raw)
        if not iso:
            return
        for i in instruments:
            date_claims.setdefault(i, set()).add(iso)

    import json as _json
    tl = base / "CASE_TIMELINE.json"
    if tl.exists():
        try:
            data = _json.loads(tl.read_text(encoding="utf-8"))
            for ev in (data.get("timeline", []) if isinstance(data, dict) else []):
                text = (ev.get("docket_ref") or "") + " " + (ev.get("notes") or "")
                inst = _f5_all_instruments((ev.get("event") or "") + " " + text)
                for m in re.finditer(r"(?i)e-?file\s*(?:no\.?|#)?\s*[:=]?\s*(\d{8,10})", text):
                    _add(m.group(1), "efile", inst)
                for m in re.finditer(r"(?i)control\s+no\.?\s*[:=]?\s*(\d{6,10})", text):
                    _add(m.group(1), "control", inst)
                for m in re.finditer(r"(?i)case\s*(?:no\.?|number)\s*[:=]?\s*(\d{6,10})", text):
                    _add(m.group(1), "case")
                for m in re.finditer(r"(?i)deemed[\s_]*filed\D{0,60}?" + r"(" + _F5_DATE + r")", text):
                    _claim(inst, m.group(1))
        except Exception:
            pass

    for dname in ("intake", "record"):
        dpath = base / dname
        if not dpath.is_dir():
            continue
        for p in dpath.rglob("*"):
            if not p.is_file() or ".bak" in p.name:
                continue
            # Filename pairing (REPLY_BRIEF_PROGRESSIVE_2610015347_20261006.pdf).
            for m in re.finditer(r"(?<!\d)(\d{10})(?!\d)", p.name):
                _add(m.group(1), "efile", _f5_all_instruments(p.name))
            if p.suffix.lower() not in (".md", ".txt") or p.stat().st_size > 400_000:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            inst = _f5_all_instruments(p.name + " " + text[:4000])
            for m in re.finditer(r"(?m)^\s*efile_no\s*:\s*[\"']?(\d{6,10})", text):
                _add(m.group(1), "efile", inst)
            for m in re.finditer(r"(?m)^\s*control_no\s*:\s*[\"']?(\d{6,10})", text):
                _add(m.group(1), "control", inst)
            for m in re.finditer(r"(?i)e-?file\s*(?:no\.?|#)?\s*[:=]?\s*(\d{8,10})", text):
                _add(m.group(1), "efile", inst)
            for m in re.finditer(r"(?i)control\s+no\.?\s*[:=]?\s*(\d{6,10})", text):
                _add(m.group(1), "control", inst)
            for m in re.finditer(r"(?i)deemed[\s_]*filed\D{0,60}?" + r"(" + _F5_DATE + r")", text[:20000]):
                _claim(inst, m.group(1))
            # Instrument tables: | paper | e-file | deemed filed | accepted |
            lines = text.splitlines()
            for i, ln in enumerate(lines):
                if not ln.lstrip().startswith("|"):
                    continue
                cells = [c.strip() for c in ln.strip("|").split("|")]
                if len(cells) >= 4 and re.fullmatch(r"\d{8,10}", cells[1] or ""):
                    paper_inst = _f5_all_instruments(cells[0])
                    _add(cells[1], "efile", paper_inst)
                    if i > 0 and "deemed" in lines[i - 1].lower():
                        dm = re.search(r"(\d{4}-\d{2}-\d{2})", cells[2])
                        if dm:
                            _claim(paper_inst, dm.group(1))
    dates = {i: next(iter(s)) for i, s in date_claims.items() if len(s) == 1}
    return numbers, efiles, dates


def _f5_slot_fill_guard(slug: str, cf_path):
    """F5 (card t_8fbf9767): reconcile every labeled e-file/control number — and the
    deemed-filed date sitting next to it — in CASE_FACTS.md against the case's own
    record. Blank beats wrong (E4). Returns updated text, or None if unchanged.
    Idempotent: a corrected number no longer matches."""
    doc = cf_path.read_text(encoding="utf-8", errors="ignore")
    numbers, efiles, dates = _f5_registry(slug)
    if not numbers:
        return None
    edits = []

    def _date_check(label_start, num_end, inst):
        """A 'filed <date>' next to this citation contradicting the record
        date — only the date span is ever replaced."""
        if inst is None or inst not in dates:
            return None
        # Date after the number, inside the same parenthetical (or 80 chars).
        tail = doc[num_end:num_end + 80]
        cp = tail.find(")")
        if cp >= 0:
            tail = tail[:cp]
        m = re.search(r"(?:deemed[\s]+)?filed\s+(?:on\s+)?" + r"(" + _F5_DATE + r")",
                      tail, re.I)
        if m:
            if _f5_iso(m.group(1)) != dates[inst]:
                return (num_end + m.start(1), num_end + m.end(1), dates[inst])
            return None
        # Date before the label: "... filed October 5, 2026 (E-File ...) ..."
        head_start = max(0, label_start - 60)
        head = doc[head_start:label_start]
        m = re.search(r"filed\s+(?:on\s+)?" + r"(" + _F5_DATE + r")", head, re.I)
        if m and _f5_iso(m.group(1)) != dates[inst]:
            return (head_start + m.start(1), head_start + m.end(1), dates[inst])
        return None

    for m in re.finditer(r"(E-?File\s*No\.?|E-?File\s*#\s*|E-?File#|E-?File)\s*(\d{8,10})",
                         doc, re.I):
        num, label = m.group(2), m.group(1)
        inst = _f5_instrument(doc[max(0, m.start() - 140):m.start()])
        info = numbers.get(num)
        if info is None:
            edits.append((m.start(2), m.end(2), "(not in record)"))
            continue
        if "efile" in info["labels"]:
            if inst is not None and info["instruments"] and inst not in info["instruments"]:
                own = efiles.get(inst)
                edits.append((m.start(2), m.end(2),
                              sorted(own)[0] if own and len(own) == 1 else "(not in record)"))
            d = _date_check(m.start(1), m.end(2), inst)
            if d:
                edits.append(d)
            continue
        # A real number the record knows only as a control/case no. — cited as an
        # e-file no. Relabel when the sentence's instrument has no e-file no. of its
        # own; otherwise swap in the instrument's authoritative e-file no.
        own = efiles.get(inst) if inst else None
        if own and len(own) == 1:
            edits.append((m.start(2), m.end(2), sorted(own)[0]))
            d = _date_check(m.start(1), m.end(2), inst)
            if d:
                edits.append(d)
        else:
            edits.append((m.start(1), m.end(1),
                          "Control No." if "control" in info["labels"] else label))

    for m in re.finditer(r"(Motion\s+Control\s+No\.?|Control\s+No\.?|Control\s*#)\s*(\d{6,10})",
                         doc, re.I):
        if m.group(2) not in numbers:
            edits.append((m.start(2), m.end(2), "(not in record)"))

    if not edits:
        return None
    out = doc
    for start, end, rep in sorted(edits, key=lambda e: -e[0]):
        out = out[:start] + rep + out[end:]
    return out


# F1 (card t_8fbf9767): an authority the OMITTED CONTROLLING AUTHORITY fold records as
# UN-CITED by the movant cannot also be graded "correctly stated" by the movant elsewhere
# in the same document. The per-section guards cannot see cross-section contradictions;
# this detects them deterministically. The model re-answers; the mark lands regardless.
def _audit_authority_consistency(doc: str) -> list:
    import re as _re
    out = []
    m = _re.search(r"(?m)^###\s+OMITTED CONTROLLING AUTHORITY\s*$(.*?)(?=^###\s|\Z)", doc, _re.S)
    if not m:
        return out
    fold_block = m.group(1)
    fold_lines = set(l.strip() for l in fold_block.splitlines())
    # per fold row: the LATER case name from its caption "FIRST PARTY v. ..."
    seen = set()
    for line in fold_block.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        cm = _re.search(r"([A-Z][A-Za-z.&'\u2019, \-]{2,60}?)\s+v\.\s+[A-Z]", s)
        if not cm:
            continue
        first = cm.group(1).strip(" ,-")
        for w in ("Appellees", "Appellants", "Plaintiffs", "Plaintiff", "Defendants", "Defendant"):
            first = _re.sub(rf"\b{w}\b", "", first)
        first = " ".join(first.replace(",", " ").split())
        if len(first) < 4 or first.lower() in seen:
            continue
        seen.add(first.lower())
        cands = {first.lower()}
        toks = [t for t in first.split() if t[0].isalpha() and len(t) > 2]
        if len(toks) >= 2:
            cands.add(" ".join(toks[-2:]).lower())
        if toks:
            cands.add(toks[-1].lower())
        # lines OUTSIDE the fold that grade the movant/brief as correctly stating it
        for line2 in doc.splitlines():
            s2 = line2.strip()
            if not s2 or s2 in fold_lines:
                continue
            if s2.startswith("### OMITTED CONTROLLING"):
                continue
            if " [CONTESTED" in s2:
                continue  # already flagged and re-answered in a prior pass
            ll = s2.lower()
            if not _re.search(r"\b(?:correctly|accurately|properly)\s+(?:states?|cites?|quotes?|summarizes?|characterizes?)\b", ll):
                continue
            if any(c in ll for c in cands):
                out.append({"case": first, "row": s, "line": s2})
    return out


def _date_role_conflicts(slug: str, block: str, ctx: str) -> list:
    """Report date/role conflicts for a bounded LLM re-ask. NEVER rewrites text.

    An earlier version substituted dates deterministically and corrupted correct
    sentences ("authorized automatic payments at policy purchase on March 21, 2019"
    became "February 1, 2026" because "payments" matched the 'pay' role). Deciding
    which event a sentence describes is comprehension, not pattern matching — so the
    guard reports and the model rewrites.
    """
    try:
        return _audit_date_roles(slug, block, ctx)
    except Exception:
        return []


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n")



# ---- WAIVER SCOPE GATE (deterministic) --------------------------------------------
# Pa.R.C.P. 1032(a) waiver is NOT total. The rule expressly EXCEPTS failure to state a
# claim, failure to join an INDISPENSABLE party (1032(b)), failure to state a legal
# defense, failure to exercise/exhaust a statutory remedy, adequate remedy at law, and
# defenses not required to be pleaded under 1030(b) -- and measures waiver against
# "preliminary objection, ANSWER OR REPLY", so a party foreclosed from further POs may
# still answer and plead new matter.
#
# m0 was told this in prose (instruction 13) and overstated it anyway: the record said
# the movant "has waived ANY objection to Count III". Prose guidance is not a guardrail.
# Overstating waiver hands the opponent a free rebuttal, so the claim is narrowed to
# what the rule actually says -- foreclosed from further PRELIMINARY OBJECTIONS.
_WAIVER_OVERCLAIM = re.compile(
    r"(?i)\bwaive[sd]?\b[^.;]{0,80}?\b(?:any|all|every)\b[^.;]{0,40}?"
    r"\b(?:objection|defense|challenge|argument)s?\b"
    r"|\b(?:any|all|every)\b[^.;]{0,40}?\b(?:objection|defense)s?\b[^.;]{0,40}?"
    r"\b(?:is|are|has been|have been|were)\s+waived\b"
    r"|\bimmune from all later\b"
    r"|\bcannot (?:now )?(?:raise|assert|argue)\b[^.;]{0,40}?\b(?:any|anything)\b"
)
# An explicit narrowing anywhere in the same sentence rescues it: the drafter already
# said which vehicle is foreclosed, so the broad word is qualified, not a defect.
_WAIVER_NARROWED = re.compile(
    r"(?i)further preliminary objection|preliminary objection[^.;]{0,30}only"
    r"|except[^.;]{0,60}(?:state a claim|indispensable)"
    r"|may still (?:answer|raise|plead)|answer or new matter|1032\(b\)"
)


def _outside_quotes(s: str) -> str:
    """The sentence with every double-quoted span blanked. Enacted rule text is QUOTED and
    1032(a) itself says 'waives any further preliminary defense' -- the gate must never read
    the rule's own words as the drafter's overclaim, nor edit inside a quotation."""
    return re.sub(r'"[^"\n]*"', lambda m: " " * len(m.group(0)), s or "")


def _waiver_scope_ok(sentence: str) -> tuple[bool, str]:
    """False when a sentence claims broader waiver than 1032(a) grants."""
    bare = _outside_quotes(sentence)
    if not _WAIVER_OVERCLAIM.search(bare):
        return True, ""
    if _WAIVER_NARROWED.search(bare):
        return True, ""
    return False, ("overstates Pa.R.C.P. 1032(a): waiver forecloses further PRELIMINARY "
                   "OBJECTIONS only; failure to state a claim and indispensable-party "
                   "defenses survive and may be raised by answer or new matter")


# The overbroad phrase itself, anchored to the waiver verb so a quoted rule or an unrelated
# "any objection" is never touched. Group 3 is the noun we keep.
_WAIVER_PHRASE = re.compile(
    r"(?i)(\bwaive[sd]?\b[^.;\"]{0,80}?)\b(any|all|every)\s+"
    r"(objection|defense|challenge|argument)(s?)\b(?!\s+further)")
_WAIVER_PHRASE_PASSIVE = re.compile(
    r"(?i)\b(any|all|every)\s+(objection|defense)(s?)\b(?=[^.;\"]{0,40}?"
    r"\b(?:is|are|has been|have been|were)\s+waived\b)")


def _narrow_outside_quotes(part: str) -> str:
    """Apply the phrase repair to unquoted spans only; quoted spans pass through verbatim."""
    spans = re.split(r'("[^"\n]*")', part)
    for i in range(0, len(spans), 2):
        spans[i] = _WAIVER_PHRASE.sub(r"\1any further preliminary \3\4", spans[i], count=1)
        spans[i] = _WAIVER_PHRASE_PASSIVE.sub(r"any further preliminary \2\3", spans[i], count=1)
    return "".join(spans)


def _scrub_waiver_overclaim(slug: str, header: str, block: str) -> tuple[str, int]:
    """Narrow over-broad waiver sentences in place. Returns (block, n_fixed).

    The sentence is repaired rather than deleted: the waiver lever is CORRECT and load
    bearing -- 1028(b) forecloses a second set of preliminary objections -- it was only
    stated too broadly. Deleting it would throw away a true argument.

    Two invariants (regression 2026-09-27): (1) text inside double quotes is never read as
    a claim and never edited -- it is the enacted rule; (2) the overbroad PHRASE is always
    narrowed when present, even if the sentence already carries the 1032(a) exception in a
    parenthetical -- the parenthetical explains the scope, it does not license the word "any".
    """
    if not block:
        return block, 0
    fixed = 0
    out = []
    for line in block.splitlines():
        if not line.strip():
            out.append(line)
            continue
        parts = _SENT_SPLIT.split(line)
        rebuilt = []
        for part in parts:
            bare = _outside_quotes(part)
            has_phrase = bool(_WAIVER_PHRASE.search(bare) or _WAIVER_PHRASE_PASSIVE.search(bare))
            ok, _ = _waiver_scope_ok(part)
            if has_phrase or not ok:
                part = _narrow_outside_quotes(part)
                if not _WAIVER_NARROWED.search(_outside_quotes(part)):
                    part = part.rstrip(". ") + (" (1032(a) excepts failure to state a "
                                                "claim and indispensable-party defenses, "
                                                "which may still be raised by answer or "
                                                "new matter)")
                fixed += 1
            rebuilt.append(part)
        joined = " ".join(x.strip() for x in rebuilt if x.strip())
        lead = re.match(r"^(\s*(?:[-*+]|\d+\.|#{1,6})\s+)", line)
        if lead and not joined.startswith(lead.group(1).strip()):
            joined = lead.group(1) + joined
        out.append(joined)
    return "\n".join(out), fixed


def _scrub_unsupported_disposition(slug: str, header: str, block: str) -> str:
    """Drop ONLY the sentences that assert an unsupported court disposition.

    Rejecting the whole block loses thousands of words of correct analysis over one bad
    sentence. Remove the offending sentences, keep everything else, then re-verify.
    """
    if not block:
        return block
    kept = []
    for line in block.splitlines():
        if not line.strip():
            kept.append(line)
            continue
        parts = _SENT_SPLIT.split(line)
        good = [p for p in parts
                if _reject_unsupported_disposition(slug, header, p)[0]]
        if not good:
            continue
        rebuilt = " ".join(x.strip() for x in good if x.strip())
        # keep list/heading markers so the markdown survives
        lead = re.match(r"^(\s*(?:[-*+]|\d+\.|#{1,6})\s+)", line)
        if lead and not rebuilt.startswith(lead.group(1).strip()):
            rebuilt = lead.group(1) + rebuilt
        kept.append(rebuilt)
    return "\n".join(kept).strip()


def refresh(slug: str, run_hermes: bool = True) -> dict:
    """Refresh one case: deterministic docket tie (the GUARDRAIL that pins the docket truth so Hermes
    can't hallucinate ECF#s), then Hermes reasons SECTION BY SECTION (bounded calls connected through
    Obsidian) so CASE_FACTS keeps full-context substance. run_hermes=False is the explicit deterministic
    pass (docket only) -- it is NOT the timeout fallback; sectioning is."""
    from pipeline import ecf_resolver
    added = ecf_resolver.tie_docket(slug, verbose=True)   # guardrail: pins the real docket #s first
    print(f"  [{slug}] tie_docket: +{len(added)} ECF# into CASE_TIMELINE.json")
    # EVIDENCE_ASSERT_V1 is for AFFIRMATIVE (pre-filing) cases: their record IS the evidence
    # set. Judicial cases' intake is docket orders — thousands of tokens, wrong gate.
    probes = _evidence_probes(slug) if _posture(slug) == "affirmative" else []
    cf_path = CASES / slug / "CASE_FACTS.md"
    _na = _write_controlling_authorities(slug, cf_path)   # L4: bake verbatim holdings at analysis time
    if _na:
        print(f"  [{slug}] controlling-authorities: {_na} verbatim authorities baked into CASE_FACTS")
    if _write_appeal_status(slug, cf_path):               # V1: derive live appeal + retire dismissed priors
        print(f"  [{slug}] appeal-status: THIRD CIRCUIT APPEAL section refreshed from docket_events.md")
    _ng = _bake_drafter_grounding(slug, cf_path)          # B: bake corpus-retrieval grounding for fast drafting
    if _ng:
        print(f"  [{slug}] drafter-grounding: {_ng} corpus block(s) baked (drafter reads these, no live retrieval)")
    sections = []
    if run_hermes:
        # Default = DIRECT completion (fast, reliable). The agentic Hermes path (BEEZLE_CF_AGENTIC=1)
        # is preserved but times out on tool-heavy sections, so it is opt-in only.
        direct = os.environ.get("BEEZLE_CF_AGENTIC") != "1"
        ctx = _section_context(slug) if direct else None
        _only = {x.strip() for x in os.environ.get("BEEZLE_CF_ONLY", "").split(",") if x.strip()}
        for i, (name, task) in enumerate(_section_tasks(slug)):
            if _only and name not in _only:
                continue
            header = _SECTION_HEADERS.get(name, f"## {name.upper()}")
            full_task = task + _OUTPUT_CONTRACT.format(header=header)
            block, ok = "", False
            try:
                if direct:
                    prompt = ("You are Beezle's deterministic legal analyst. Using ONLY the case record "
                              "below (it is already corpus-verified — do not invent facts or citations "
                              "beyond it), produce the requested markdown block.\n\n"
                              f"{ctx}\n\n=== YOUR TASK ===\n{full_task}")
                    # max_tokens 14000 up front: the section-level completion carries the
                    # premise audit + the full rebuttal; 4000 let the thinking budget eat
                    # the completion on the 2026-10-06 reply-brief run (empty content).
                    out = _call_direct(prompt, max_tokens=14000, timeout=900)
                else:
                    cmd = [HERMES, "--provider", PROVIDER, "--model", MODEL, "--yolo"] \
                        + (["-c"] if i else []) + ["-s", SKILL, "-z", full_task]
                    out = subprocess.run(cmd, timeout=SECTION_TIMEOUT,
                                         capture_output=True, text=True).stdout or ""
                block = _extract_block(out, header)
                # DATE-ROLE AUDIT: a date attached to the wrong event states a fact the
                # record contradicts. Learned from the case's own timeline, so it works
                # on any case without configuration.
                try:
                    _dr = _audit_date_roles(slug, block, ctx)
                    for _p in _dr[:4]:
                        print(f"  [{slug}] DATE-ROLE [{name}]: {_p}")
                    # BOUNDED RE-ASK: hand the conflict back to the model, which can
                    # read the sentence. Never substitute dates mechanically.
                    if _dr and direct:
                        _conf = "; ".join(x.split(":")[0] for x in _dr[:4])
                        _fix_prompt = (
                            f"{ctx}\n\n=== YOUR TASK ===\n{full_task}\n\n"
                            f"=== RECORD CONFLICT — FIX BEFORE ANSWERING ===\n"
                            f"Your previous draft attached a date to the wrong event: {_conf}. "
                            f"Re-read the record above and state each event with ITS OWN date. "
                            f"Do not change any date that is already correct; a date may "
                            f"legitimately appear with more than one event in the same sentence. "
                            f"Return the same markdown block, corrected.")
                        try:
                            _retry = _extract_block(_call_direct(_fix_prompt), header)
                            if _retry and len(_retry) >= 0.6 * len(block):
                                _dr2 = _audit_date_roles(slug, _retry, ctx)
                                if len(_dr2) < len(_dr):
                                    print(f"  [{slug}] DATE-ROLE RE-ASK [{name}]: "
                                          f"{len(_dr)} -> {len(_dr2)} conflict(s)")
                                    block = _retry
                                else:
                                    print(f"  [{slug}] DATE-ROLE RE-ASK [{name}]: "
                                          f"no improvement ({len(_dr2)}), keeping original")
                        except CorpusLocked:
                            raise
                        except Exception as _re:
                            print(f"  [{slug}] date-role re-ask skipped: {_re}")
                except CorpusLocked:
                    raise
                except Exception as _e:
                    print(f"  [{slug}] date-role audit skipped (non-fatal): {_e}")
                # m2 COVERAGE GUARD (F3 2026-10-21): findings m2 raised that the block never answers.
                if block and direct and name in ("aff_claims", "aff_damages"):
                    try:
                        _un = _m2_unanswered(slug, name, block)
                        if _un:
                            for _c, _ax, _ms, _g in _un[:6]:
                                print(f"  [{slug}] m2-UNANSWERED [{name}] Count {_c} [{_ax}]: {', '.join(_ms)}")
                            _ask = "\n".join(f"- Count {_c} [{_ax}] (names {', '.join(_ms)}): {_g}"
                                              for _c, _ax, _ms, _g in _un[:10])
                            _retry = _extract_block(_call_direct(
                                f"{ctx}\n\n=== YOUR TASK ===\n{full_task}\n\n=== YOUR PREVIOUS DRAFT ===\n{block}\n\n"
                                f"=== m2 FINDINGS YOUR DRAFT DID NOT ANSWER -- FIX BEFORE ANSWERING ===\n{_ask}\n"
                                f"For each: if it is a defense/immunity/element against that count, the count must meet "
                                f"it by name (plead the record facts that take the claim outside it, or narrow the "
                                f"count); if it does not apply to this forum or posture, say so in one line by name. "
                                f"Keep everything else in your previous draft. Return the same markdown block.",
                                max_tokens=14000, timeout=900), header)
                            if _retry and len(_retry) >= 0.6 * len(block):
                                _un2 = _m2_unanswered(slug, name, _retry)
                                print(f"  [{slug}] m2-UNANSWERED RE-ASK [{name}]: {len(_un)} -> {len(_un2)}")
                                if len(_un2) < len(_un):
                                    block = _retry
                    except CorpusLocked:
                        raise
                    except Exception as _me:
                        print(f"  [{slug}] m2 coverage guard skipped (non-fatal): {_me}")
                # COVERAGE AUDIT: print what the analysis dropped from its own context.
                if block:
                    # RELABEL FOLD: rule 10 asks m0 to answer from OUR pleading; it
                    # adopted the movant's substituted label anyway. Guarantee the
                    # non-responsiveness point instead of hoping for it.
                    if name == "rebuttal":
                        # fire on THEIR mismatch, not on our drift: the objection is
                        # non-responsive whenever it attacks an unpleaded label
                        _rb = _adopts_their_label(slug, block)
                        if not _rb:
                            # The non-responsiveness point does not depend on OUR
                            # analysis having drifted into their label -- it depends on
                            # the MOVANT demurring to a label the Complaint never
                            # pleaded. Same source the relabel fold already uses.
                            _rb = _mischaracterizations(slug)
                        if _rb:
                            _before = block
                            block = _fold_relabel_answer(slug, block, _prior_doc(cf_path))
                            if block == _before:
                                print(f"  [{slug}] RELABEL FOLD [{name}]: SKIPPED — "
                                      f"already present in block")
                                _rb = []
                            for _n, _o, _t in _rb:
                                print(f"  [{slug}] RELABEL FOLD [{name}]: Count {_n} "
                                      f"pleaded as \"{_o[:46]}\" but the analysis adopted "
                                      f"the movant's \"{_t}\" — non-responsiveness point "
                                      f"folded in")
                    # RECORD FOLD before the audit: rule 18 asks, this guarantees.
                    if name == "rebuttal":
                        _before = len(block)
                        _nb_before = block
                        block = _fold_nonbinding_authority(slug, block, _prior_doc(cf_path))
                        if block != _nb_before:
                            print(f"  [{slug}] NON-BINDING FOLD [{name}]: the movant's "
                                  f"non-binding authority answered with the rule from "
                                  f"corpus")
                        block = _fold_record_facts(slug, block)
                        _ds_before = block
                        # Am Jur: a count under demurrer with no stated elements cannot
                        # be defended. Build the chains before the coverage fold reads.
                        try:
                            _cur = Path(cf_path).read_text(encoding="utf-8")
                            _new = _build_missing_element_chains(slug, _cur)
                            if _new != _cur:
                                Path(cf_path).write_text(_new, encoding="utf-8")
                        except CorpusLocked:
                            raise
                        except Exception as _ce:
                            print(f"  [{slug}] ELEMENT CHAIN BUILD failed: {_ce}")
                        block = _fold_element_coverage(slug, block,
                                                      _prior_doc(cf_path))
                        block = _fold_breach_origin(slug, block)
                        block = _verify_their_characterizations(slug, block)
                        _pv_before = block
                        block = _fold_adverse_authority_provenance(slug, block, _prior_doc(cf_path))
                        if block != _pv_before:
                            print(f"  [{slug}] PROVENANCE FOLD [{name}]: the movant's own "
                                  f"participation in its cited authorities recorded from corpus")
                        _om_before = block
                        block = _fold_omitted_controlling_authority(slug, block, _prior_doc(cf_path))
                        if block != _om_before:
                            print(f"  [{slug}] OMITTED-AUTHORITY FOLD [{name}]: binding later authority "
                                  f"the movant did not cite recorded from corpus")
                        block = _fold_distinctions(slug, block, _prior_doc(cf_path))
                        # re-assert: a later fold may have recomposed the block from the
                        # model text and dropped the non-responsiveness answer
                        if _adopts_their_label(slug, block):
                            block = _fold_relabel_answer(slug, block,
                                                         _prior_doc(cf_path))
                        # same hazard for the element chains: re-run the coverage fold and
                        # keep whichever version carries chains for MORE counts, so a
                        # recomposition cannot quietly drop a count's chain
                        try:
                            _c_before = len(re.findall(
                                r"(?m)^- \*\*Count [IVX]+ .*every element is met", block))
                            _re_cov = _fold_element_coverage(slug, block,
                                                             _prior_doc(cf_path))
                            _c_after = len(re.findall(
                                r"(?m)^- \*\*Count [IVX]+ .*every element is met",
                                _re_cov))
                            if _c_after > _c_before:
                                print(f"  [{slug}] ELEMENT CHAIN RE-ASSERT: "
                                      f"{_c_before} -> {_c_after} count(s) carried")
                                block = _re_cov
                        except CorpusLocked:
                            raise
                        except Exception as _re_e:
                            print(f"  [{slug}] ELEMENT CHAIN RE-ASSERT failed: {_re_e}")
                        if block != _ds_before:
                            print(f"  [{slug}] DISTINCTION FOLD [{name}]: adverse "
                                  f"authorities distinguished on the record")
                        if len(block) > _before:
                            print(f"  [{slug}] RECORD FOLD [{name}]: "
                                  f"+{len(block)-_before} chars of record facts the "
                                  f"analysis argued without")
                    # AUTHORITY FOLD: the audit below has been reporting dropped
                    # authority for runs on end. Close the gap instead of noting it.
                    if name == "rebuttal":
                        _am = _required_authorities(slug, block, ctx or "")
                        if _am:
                            block = _fold_authorities(slug, block, ctx or "", _prior_doc(cf_path))
                            print(f"  [{slug}] AUTHORITY FOLD [{name}]: restored "
                                  f"{len(_am)} dropped authority(ies): "
                                  f"{', '.join(c for c, _n, _r in _am[:6])}")
                    # QUOTED-RULE + BARE-CITE GATES: a misquoted rule and a cite with no
                    # holding are both defects m1 copies faithfully into the brief, so they
                    # must be caught where the analysis is WRITTEN.
                    try:
                        block = _fold_quoted_rule_gate(slug, block)
                    except CorpusLocked:
                        raise
                    except Exception as _qe:
                        print(f"  [{slug}] rulequote gate failed: {_qe}")
                    try:
                        block = _fold_bare_cite_gate(slug, block)
                    except CorpusLocked:
                        raise
                    except Exception as _be:
                        print(f"  [{slug}] barecite gate failed: {_be}")
                    for _g in _audit_analysis_coverage(slug, name, block, ctx or ""):
                        print(f"  [{slug}] analysis GAP [{name}]: {_g}")
                # RULE-DEFECT PRODUCER: compute the movant's own local-rule defects
                # from the court's rule contract. Runs BEFORE fold-assert so a fresh
                # case with no prior record still gets the section.
                if block and name == "rebuttal":
                    try:
                        block = _fold_rule_defects(slug, block, _prior_doc(cf_path))
                    except CorpusLocked:
                        raise
                    except Exception as _rde:
                        print(f"  [{slug}] rule-defect fold failed: {_rde}")
                # FOLD ASSERT: restore contracted sections a rewrite dropped.
                if block:
                    try:
                        block = _fold_assert_sections(slug, header, block, _prior_doc(cf_path))
                    except CorpusLocked:
                        raise
                    except Exception as _fae:
                        print(f"  [{slug}] fold-assert failed: {_fae}")
                # DISPOSITION GUARD (section-agnostic): a block may not assert a court
                # ruling unless an entered order actually exists in the record.
                if block:
                    _allowed, _why = _reject_unsupported_disposition(slug, header, block)
                    if not _allowed:
                        # SCRUB, don't discard: one bad sentence must not cost the whole
                        # section. Remove the offending sentences and re-verify.
                        _scrubbed = _scrub_unsupported_disposition(slug, header, block)
                        _ok2, _ = _reject_unsupported_disposition(slug, header, _scrubbed)
                        if _scrubbed and _ok2 and len(_scrubbed) >= 0.5 * len(block):
                            print(f"  [{slug}] section [{name}] SCRUBBED (kept "
                                  f"{len(_scrubbed)}/{len(block)} chars): {_why[:90]}")
                            block = _scrubbed
                        else:
                            print(f"  [{slug}] section [{name}] REJECTED: {_why}")
                            block = ""
                # WAIVER SCOPE GATE: 1032(a) waiver is not total. Narrow any sentence
                # claiming broader waiver than the rule grants (prose instruction 13 was
                # not enough -- the LLM wrote "waived ANY objection" anyway).
                if block:
                    block, _nfix = _scrub_waiver_overclaim(slug, header, block)
                    if _nfix:
                        print(f"  [{slug}] section [{name}] WAIVER-SCOPE narrowed "
                              f"{_nfix} overbroad claim(s) to 1032(a)'s actual reach")
                # ok ONLY if the LLM returned the block AND Python verified it landed in CASE_FACTS.
                ok = bool(block) and _upsert_block(cf_path, header, block)
            except CorpusLocked:
                raise
            except Exception as e:
                print(f"  [{slug}] section [{name}] error: {type(e).__name__}: {e}"
                      + ("  -- CORPUS LOCKED: record NOT written; rerun when the writer finishes"
                         if isinstance(e, CorpusLocked) else ""))
                ok = False
            sections.append((name, ok))
            print(f"  [{slug}] case-facts section [{name}]: "
                  f"{'ok (wrote %d chars)' % len(block) if ok else 'FAIL (no block persisted)'}")
        # CROSS-SECTION CONSISTENCY: per-section guards cannot see a contradiction
        # between two sections. Re-ask the model to reconcile; never rewrite mechanically.
        try:
            _doc = cf_path.read_text(encoding="utf-8", errors="ignore")
            # GROUNDING first: a period every section agrees on can still be wrong, and
            # cross-section cannot see that. Report ungrounded starts as conflicts too so
            # the same sentence-level reconcile repairs them.
            _gr = _audit_period_grounding(slug, _doc)
            for _p in _gr[:4]:
                print(f"  [{slug}] PERIOD UNGROUNDED: {_p[:150]}")
            _xs = _audit_cross_section(_doc) + _gr
            for _p in _xs[:4]:
                print(f"  [{slug}] CROSS-SECTION [{'?'}]: {_p[:150]}")
            if _xs and direct:
                # SENTENCE-LEVEL reconcile. Asking for a whole block back makes the model
                # echo its own context (which still contains the bad sentence) — it
                # returned the block byte-identical. Quote the offending sentence, ask
                # for ONE corrected sentence, and splice that. The MODEL decides which
                # period the record supports; the splice is mechanical.
                _ctx3 = _section_context(slug)
                for _attempt in range(3):
                    _cl = _period_claims(_doc)
                    _bad = _oth = None
                    # PRIORITY 1 — a period the SOURCE RECORD does not support. Counting
                    # how many sections repeat a date measures agreement, not truth: the
                    # wrong date can be the majority. Grounding decides.
                    _ung = _audit_period_grounding(slug, _doc)
                    _ung_starts = set()
                    for _u in _ung:
                        _mu = re.search(r"period start '([^']+)'", _u)
                        if _mu:
                            _ung_starts.add(_norm_date(_mu.group(1)) or _mu.group(1))
                    if _ung_starts:
                        for _c in _cl:
                            if _c["start"] in _ung_starts:
                                _bad = _c
                                # prefer a grounded rival describing the same quantity
                                _oth = next((_o for _o in _cl
                                             if _o["start"] not in _ung_starts
                                             and len(_o["key"] & _c["key"]) >= 1), None)
                                break
                    # PRIORITY 2 — two sections disagree and both are grounded.
                    if not _bad:
                        for _i, _a in enumerate(_cl):
                            for _b in _cl[_i + 1:]:
                                if len(_a["key"] & _b["key"]) >= 2 and _a["start"] != _b["start"]:
                                    _bad, _oth = _a, _b
                                    break
                            if _bad:
                                break
                    if not _bad:
                        break
                    # candidate starts the RECORD supports, with the event each names
                    _cand_roles = _date_roles_from_record(_doc)
                    _cands = []
                    for _d, _c3 in sorted(_cand_roles.items()):
                        _r3 = _dominant_roles(_c3) & {"dispose", "cancel", "acquire"}
                        if not _r3:
                            continue
                        _y, _mo, _dy = _d.split("-")
                        _mn = ("January February March April May June July August "
                               "September October November December").split()
                        _lbl = f"{_mn[int(_mo) - 1]} {int(_dy)}, {_y}"
                        _alt = f"{_mo}/{_dy}/{_y}"
                        # the record's own words for this date, so the model can tell
                        # an event from a later administrative step with the same role
                        _ev = ""
                        for _ln in _doc.splitlines():
                            if (_lbl in _ln or _alt in _ln) and len(_ln.strip()) > 25:
                                _ev = " ".join(_ln.split())
                                _ev = re.sub(r"^[\s*\-#>]+", "", _ev)
                                _ev = re.sub(r"\(\*?Source:.*$", "", _ev).strip()
                                if len(_ev) > 150:
                                    _ev = _ev[:150].rsplit(" ", 1)[0] + "..."
                                break
                        _cands.append(f"{_lbl} ({'/'.join(sorted(_r3))})"
                                      + (f" -- record: {_ev}" if _ev else ""))
                    _cand_txt = ("\n".join(f"  - {x}" for x in _cands)
                                 if _cands else "  (none identified)")
                    _ask = (f"{_ctx3}\n\n=== YOUR TASK ===\n"
                            f"This sentence appears in the case file:\n\n\"{_bad['line']}\"\n\n"
                            f"A period for money owed must START on the date the "
                            f"OBLIGATION AROSE -- the underlying event itself, not a later "
                            f"step that merely evidences, reports, or processes it. In "
                            f"THIS record the candidate dates are:\n{_cand_txt}\n\n"
                            f"Use one of those dates, or leave the sentence unchanged if it "
                            f"is already correct. Do NOT introduce any other date.\n\n"
                            + (f"It says the period starts {_bad['start_raw']}. Elsewhere "
                               f"the same quantity is stated as starting "
                               f"{_oth['start_raw']}: \"{_oth['line'][:200]}\"\n\n"
                               if _oth else
                               f"It says the period starts {_bad['start_raw']}, but the "
                               f"source record never ties that date to this quantity.\n\n")
                            + f"Using ONLY the record above, decide which start date is correct "
                            f"and rewrite the FIRST sentence so it is accurate. Change nothing "
                            f"else in it — same facts, same exhibit references, same wording "
                            f"except the date(s) that are wrong.\n"
                            f"Reply with the corrected sentence ONLY, on one line, no "
                            f"commentary, no markdown fence.")
                    try:
                        _fix = (_call_direct(_ask) or "").strip()
                    except Exception as _ce:
                        print(f"  [{slug}] CROSS-SECTION re-ask call failed: {_ce}")
                        break
                    _fix = _fix.strip("`").strip()
                    _fix = re.sub(r"^(?:corrected sentence|answer)\s*:\s*", "", _fix, flags=re.I).strip()
                    _fix = _fix.splitlines()[0].strip() if _fix else ""
                    if not _fix or len(_fix) < 0.5 * len(_bad["line"]):
                        print(f"  [{slug}] CROSS-SECTION re-ask: unusable reply, keeping original")
                        break
                    if _fix == _bad["line"]:
                        print(f"  [{slug}] CROSS-SECTION re-ask: model kept the sentence as-is")
                        break
                    _probe = _doc.replace(_bad["line"], _fix)
                    # Any other sentence stating the SAME quantity with the SAME rejected
                    # start date must move too, or the file simply trades one defect for
                    # an internal contradiction.
                    _new_start = None
                    _mfix = _DATE_TOKEN.search(_fix)
                    if _mfix:
                        _new_start = _mfix.group(0)
                    if _new_start and _bad.get("start_raw"):
                        for _c4 in _period_claims(_probe):
                            if _c4["start"] != _bad["start"]:
                                continue
                            if len(_c4["key"] & _bad["key"]) < 1:
                                continue
                            _probe = _probe.replace(
                                _c4["line"],
                                _c4["line"].replace(_c4["start_raw"], _new_start))
                    if _probe == _doc:
                        # the line was reflowed in the file; match on its distinctive head
                        _head = _bad["line"][:60]
                        _idx = _doc.find(_head)
                        if _idx < 0:
                            print(f"  [{slug}] CROSS-SECTION re-ask: sentence not locatable")
                            break
                        _eol = _doc.find("\n", _idx)
                        _probe = _doc[:_idx] + _fix + _doc[_eol if _eol > 0 else len(_doc):]
                    _after = len(_audit_cross_section(_probe)) + \
                        len(_audit_period_grounding(slug, _probe))
                    if _after < len(_xs):
                        cf_path.write_text(_probe, encoding="utf-8")
                        _doc = _probe
                        _xs = _audit_cross_section(_doc) + _audit_period_grounding(slug, _doc)
                        print(f"  [{slug}] CROSS-SECTION RECONCILED: "
                              f"{_bad['start_raw']} -> corrected; {len(_xs)} conflict(s) remain")
                        if not _xs:
                            break
                    else:
                        print(f"  [{slug}] CROSS-SECTION re-ask: no improvement "
                              f"({len(_xs)} -> {_after}), keeping original")
                        break
        except Exception as _xe:
            print(f"  [{slug}] cross-section audit skipped (non-fatal): {_xe}")
        # F1 AUTHORITY CONSISTENCY (card t_8fbf9767): the cross-section contradiction in
        # its authority form — the premise audit grades a case 'correctly stated' while
        # the same document's OMITTED CONTROLLING fold records that case as uncited.
        # Deterministic CONTESTED mark first (it lands even if the re-ask fails), then
        # one bounded model re-ask per conflict re-answering the lawyer's question with
        # the omitted authority's holding quoted.
        try:
            _doc = cf_path.read_text(encoding="utf-8", errors="ignore")
            _ac = _audit_authority_consistency(_doc)
            for _p in _ac[:4]:
                print(f"  [{slug}] AUTHORITY-CONTESTED: {_p['line'][:100]}")
            if _ac:
                _mark = " [CONTESTED — the same block records this authority as omitted controlling]"
                for _p in _ac[:4]:
                    if _p["line"] in _doc and (_p["line"] + _mark) not in _doc:
                        _doc = _doc.replace(_p["line"], _p["line"] + _mark, 1)
                        _p["marked"] = True
                if direct:
                    for _p in _ac[:2]:
                        _ask = (f"{_section_context(slug)}\n\n=== YOUR TASK ===\n"
                                f"This line in the case file:\n\"{_p['line']}\"\n\n"
                                f"contradicts this fold row in the SAME document:\n{_p['row']}\n\n"
                                f"Answer the lawyer's question: on the facts as pleaded in THIS "
                                f"complaint, what did the later binding court hold about the "
                                f"proposition the movant is advancing, and did the movant's "
                                f"proposition survive there? Quote the later court's holding "
                                f"from corpus. Rewrite the FIRST line so it no longer contradicts "
                                f"the fold — keep its facts, change only its verdict. Reply with "
                                f"the corrected line ONLY: one line, no markdown, no preamble.")
                        try:
                            _fix = (_call_direct(_ask, max_tokens=1000, timeout=300) or "").strip().strip("`").strip()
                            _fix = re.sub(r"^(?:corrected line|answer)\s*:\s*", "", _fix, flags=re.I).strip()
                            _fix = _fix.splitlines()[0].strip() if _fix else ""
                        except Exception as _ae:
                            print(f"  [{slug}] AUTHORITY re-ask call failed: {_ae}")
                            break
                        _target = _p["line"] + _mark if _p.get("marked") else _p["line"]
                        if _fix and len(_fix) >= 0.4 * len(_p["line"]) and _fix.lower() != _p["line"].lower():
                            if _target in _doc:
                                _doc = _doc.replace(_target, _fix + _mark, 1)
                                print(f"  [{slug}] AUTHORITY re-ask: line re-answered with the later holding")
                            else:
                                print(f"  [{slug}] AUTHORITY re-ask: line not locatable, CONTESTED mark stands")
                        else:
                            print(f"  [{slug}] AUTHORITY re-ask: unusable reply, CONTESTED mark stands")
                cf_path.write_text(_doc, encoding="utf-8")
        except CorpusLocked:
            raise
        except Exception as _ace:
            print(f"  [{slug}] authority-consistency audit skipped (non-fatal): {_ace}")
        # F4 SWORN FACTS (card t_8fbf9767) — deterministic counterpart to premise-audit
        # Q10_sworn_facts: if the movant's paper answered our sworn facts with silence,
        # the case file says so regardless of the model's attention. Non-fatal.
        try:
            _f4 = _sworn_facts_audit(slug, cf_path)
            if _f4:
                cf_path.write_text(_f4, encoding="utf-8")
                print(f"  [{slug}] SWORN FACTS UNANSWERED fold written (deterministic, 1029(b))")
        except Exception as _f4e:
            print(f"  [{slug}] sworn-facts audit skipped (non-fatal): {_f4e!r}")
        # F5 SLOT-FILL (card t_8fbf9767): docket numbers must come from the case's own
        # record — blank beats wrong (E4). Non-fatal.
        try:
            _f5 = _f5_slot_fill_guard(slug, cf_path)
            if _f5 is not None:
                cf_path.write_text(_f5, encoding="utf-8")
                print(f"  [{slug}] SLOT-FILL: docket numbers reconciled against the record")
        except Exception as _f5e:
            print(f"  [{slug}] slot-fill guard skipped (non-fatal): {_f5e!r}")
        _connect_notes()   # connect the pieces through the Obsidian graph
    # EVIDENCE_ASSERT_V1 gate: every distinctive token from newer-than-CASE_FACTS evidence
    # must now appear in CASE_FACTS. One bounded retry re-runs aff_facts with the missing
    # facts INJECTED; still-missing facts are appended deterministically (record quotes
    # never depend on the model agreeing to include them).
    if run_hermes and probes:
        missing = _assert_evidence_absorbed(slug, probes)
        if missing:
            print(f"  [{slug}] EVIDENCE_ASSERT: {len(missing)}/{len(probes)} probe fact(s) NOT absorbed — bounded retry")
            miss_lines = "; ".join(f"{t} (from {f})" for f, t in missing[:12])
            header = _SECTION_HEADERS.get("aff_facts", "## OPERATIVE FACTS")
            retry_task = (f"The refreshed '## OPERATIVE FACTS' section of obsidian_sync/cases/{slug}/CASE_FACTS.md "
                          f"OMITTED these verbatim record facts from newer intake evidence: {miss_lines}. "
                          f"Rewrite '## OPERATIVE FACTS' to include EVERY one, each quoted with its source file. "
                          f"Preserve the section's existing accurate content."
                          + _OUTPUT_CONTRACT.format(header=header))
            try:
                ctx2 = _section_context(slug)
                prompt2 = ("You are Beezle's deterministic legal analyst. Using ONLY the case record "
                           "below (it is already corpus-verified — do not invent facts or citations "
                           "beyond it), produce the requested markdown block.\n\n"
                           f"{ctx2}\n\n=== YOUR TASK ===\n{retry_task}")
                out2 = _call_direct(prompt2)
                block2 = _extract_block(out2, header)
                if block2:
                    _upsert_block(CASES / slug / "CASE_FACTS.md", header, block2)
            except Exception as e:
                print(f"  [{slug}] EVIDENCE_ASSERT retry error: {e}")
            still = _assert_evidence_absorbed(slug, probes)
            if still:
                cfp = CASES / slug / "CASE_FACTS.md"
                if cfp.exists():
                    body = cfp.read_text(encoding="utf-8", errors="ignore")
                    lines = ["", "### RECORD FACTS PENDING NARRATIVE INTEGRATION (auto-appended by EVIDENCE_ASSERT_V1; verbatim from intake)", ""]
                    for f, t in still:
                        lines.append(f"- `{t}` — from intake evidence `{f}` (newer than last full refresh; integrate on next pass)")
                    if "### RECORD FACTS PENDING NARRATIVE INTEGRATION" not in body:
                        hdr = "## OPERATIVE FACTS"
                        if hdr in body:
                            nxt = body.find("\n## ", body.find(hdr) + len(hdr))
                            insert_at = nxt if nxt != -1 else len(body)
                            body = body[:insert_at] + "\n".join(lines) + "\n" + body[insert_at:]
                        else:
                            body = body.rstrip() + "\n" + "\n".join(lines) + "\n"
                        cfp.write_text(body, encoding="utf-8")
                print(f"  [{slug}] EVIDENCE_ASSERT: {len(still)} fact(s) deterministically appended (LLM narrative left them out)")
            else:
                print(f"  [{slug}] EVIDENCE_ASSERT: all probe facts absorbed after retry")
        else:
            print(f"  [{slug}] EVIDENCE_ASSERT: all {len(probes)} probe fact(s) absorbed")
    return {"slug": slug, "tied": added, "sections": sections}


def main() -> int:
    args = sys.argv[1:]
    run_hermes = "--no-hermes" not in args
    targets = [a for a in args if not a.startswith("--")]
    slugs = targets or _cases_needing_refresh()
    if not slugs:
        print("case_facts_refresh: no cases with intake newer than CASE_FACTS.md")
        return 0
    print(f"case_facts_refresh: {len(slugs)} case(s) -> {slugs}")
    for slug in slugs:
        refresh(slug, run_hermes=run_hermes)
    return 0


# ── ENTERED-ORDER DETECTOR (deterministic guardrail behind the latest_order prompt) ──
# A proposed order attached to a party's motion is the MOVANT'S REQUESTED relief, never a
# disposition. Caught live 2026-09-23: Progressive's preliminary objections carried a
# proposed order ("Preliminary Objections are GRANTED ... DISMISSED with prejudice") and
# the refresh wrote it into CASE_FACTS as an entered ruling. The LLM prompt now says so;
# this function is the check that does not depend on the model complying.
_PROPOSED_TELLS = (
    r"NOW,?\s*th(?:is|e)\s+_{2,}",              # "NOW, this ____ day of"
    r"day\s+of\s+_{2,}",
    r"_{5,}\s*,?\s*J\.",                          # unsigned "______ J."
    r"\[PROPOSED\]",
    r"\bPROPOSED\s+ORDER\b",
)
_ENTERED_TELLS = (
    # a SIGNED judicial line: /s/ or a real name — an unsigned "BY THE COURT:\n___ J."
    # (the proposed-order shape) must NOT match.
    r"BY\s+THE\s+COURT:\s*\n?\s*/s/\s*[A-Za-z]",
    r"BY\s+THE\s+COURT:\s*\n?\s*(?!_)[A-Z][a-z]+\s+[A-Z]",
    r"\bSO\s+ORDERED\b",
    r"/s/\s*[A-Z][A-Za-z.\- ]+,\s*J\.",
)
_MOVANT_TELLS = (
    r"Control\s*No\.?\s*[:#]?\s*\d+",
    r"Respectfully\s+submitted",
    r"Counsel\s+for\s+(?:Defendant|Plaintiff|Petitioner|Respondent)",
    r"PRELIMINARY\s+OBJECTIONS|MOTION\s+TO\s+DISMISS|BRIEF\s+IN\s+SUPPORT",
)


def is_entered_order(text: str) -> dict:
    """Classify a document: entered order vs a party filing carrying a proposed order.

    Returns {"entered": bool, "kind": str, "why": str}. Conservative by design — when the
    signals conflict, a document showing movant tells and no signature is NOT an order.
    """
    import re as _re
    t = text or ""
    head = t[:20000]
    prop = [p for p in _PROPOSED_TELLS if _re.search(p, head, _re.I)]
    ent = [p for p in _ENTERED_TELLS if _re.search(p, head, _re.I)]
    mov = [p for p in _MOVANT_TELLS if _re.search(p, head, _re.I)]
    if prop and not ent:
        return {"entered": False, "kind": "proposed_order_in_party_filing",
                "why": f"proposed-order tells present ({len(prop)}), no judicial signature; "
                       f"movant tells={len(mov)}"}
    if mov and not ent:
        return {"entered": False, "kind": "party_filing",
                "why": f"party-filing tells present ({len(mov)}), no judicial signature"}
    if ent:
        return {"entered": True, "kind": "entered_order",
                "why": f"judicial execution present ({len(ent)})"}
    return {"entered": False, "kind": "indeterminate",
            "why": "no judicial signature found; treat as NOT entered until verified"}


if __name__ == "__main__":
    raise SystemExit(main())
