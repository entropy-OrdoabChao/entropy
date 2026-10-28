#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/issue_spotter.py — the DEPTH issue-spotter, productionized.

Replaces m0's shallow detect->map->render ceiling for lever DISCOVERY. The LLM PROPOSES candidate
doctrines/levers open-endedly (who/what/when/where/why over the record), a DEEPEN pass pushes past the
mainstream doctrine to the sharper one, and a deterministic GATE disposes: each candidate must resolve
to real authority — case cite -> Harvard-first case_authority (existence + BINDING); rule/statute ->
rules_index; else corpus semantic + corpus_graph. Survivors are written back to the connected-notes
graph (flywheel) so the next pass surfaces them unaided.

R7-safe: the LLM proposes and polishes; the corpus/gate DECIDE what is real. Nothing ungrounded survives.

Public:
  spot_case(case_id, rounds=1, write_back=True) -> list[dict]   # validated levers
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, json, glob
from pathlib import Path

ROOT = Path(os.environ.get("BEEZLE_ROOT", str(_eroot())))
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

def _case_folder(case_id: str) -> str:
    cid = (case_id or "").strip()
    if cid.startswith("fed:"):
        return "fed_" + cid.split(":", 2)[-1]
    return cid.replace(":", "_")

def _target_court(case_id: str) -> str:
    return "EDPA" if "fed" in (case_id or "") else "PA_CP"

def _read(p, n=None):
    try:
        t = Path(p).read_text(encoding="utf-8", errors="ignore"); return t[:n] if n else t
    except Exception:
        return ""

def _chunks(s, size):
    return [s[i:i + size] for i in range(0, len(s), size)] or [""]

_SYS = ("You are a diligent litigator ISSUE-SPOTTING for a plaintiff, not asserting law. The whole legal "
        "corpus stands behind you. From the SECTION, list EVERY doctrine, rule, defect, or lever a careful "
        "lawyer would consider on THIS record — do NOT stop at the obvious; run who/what/when/where/why on "
        "each fact. When a self-represented party's arguments/evidence are NOT individually addressed, reason "
        "about what it means for the party's CAPACITY/STATUS and the court's DUTIES. Give the rule/statute/"
        "case you believe supports each, or 'VERIFY'. Output ONLY a JSON array of "
        '{"lever","doctrine","cite","why","record_tie"} — no prose outside JSON.')

_DEEP_SYS = ("You are a litigator who NEVER stops at the mainstream doctrine. Given a lever another lawyer "
             "found, go DEEPER: the SHARPER, more fundamental lever the corpus supports beyond it — the "
             "controlling DEFINITION, the precise CAPACITY/STATUS characterization, or the MANDATORY duty "
             "leaving no discretion. Also CHECK the cite: is it BINDING in this court? If not, name the "
             "binding equivalent. Give rule/statute/case or 'VERIFY'. Output ONLY a JSON array of "
             '{"lever","doctrine","cite","why","record_tie"} — no prose outside JSON.')

_REPCITE = re.compile(r"(\d{1,3})\s+(U\.?\s?S\.?|S\.?\s?Ct\.?|F\.?\s?[23]?d?|F\.?\s?Supp\.?\s?[23]?d?|A\.?\s?[23]?d?)\s+(\d{1,4})", re.I)
_RULE_RE = re.compile(r"(Pa\.\s?R\.|Fed\.\s?R\.|F\.?R\.?C\.?P|FRCP|FRE|FRAP|U\.?S\.?C|C\.?F\.?R|Local\s+Rule|§)", re.I)

def _split_cite(s):
    m = _REPCITE.search(s or "")
    return ((s[:m.start()].strip(" ,"), m.group(0)) if m else ((s or "").strip(" ,"), ""))

def _parse(raw):
    raw = raw or ""
    if "</think>" in raw:
        raw = raw.split("</think>")[-1]
    out = []
    for o in re.findall(r"\{[^{}]*\}", raw, re.S):
        try:
            d = json.loads(o)
            if isinstance(d, dict) and d.get("lever"):
                out.append(d)
        except Exception:
            pass
    return out

def _llm(user, system, max_tokens=5000):
    # thinking-on eats tokens before the JSON array; keep the cap high enough that the array is not
    # truncated (3200 truncated the section arrays to ~4 parsed levers vs ~40 at 5000).
    from pipeline.motion_drafter_v2 import call_llm
    return _parse(call_llm(user, system=system, max_tokens=max_tokens, temperature=0.3, enable_thinking=True))

def _validate(cand, target_court):
    """The gate: case cite -> Harvard-first authority (+BINDING); rule/statute -> rules_index; else corpus."""
    cite = (cand.get("cite") or "").strip()
    doct = (cand.get("doctrine") or cand.get("lever") or "")
    name, repcite = _split_cite(cite)
    if repcite and " v. " in cite.lower():
        try:
            from pipeline.case_fetch import case_authority
            a = case_authority(name, repcite, target_court)
            if a.get("found"):
                return "case-" + a["binding"], (a.get("level") or a.get("provider") or "")
        except Exception:
            pass
        return "case-UNVERIFIED", ""
    if cite and cite.upper() != "VERIFY" and _RULE_RE.search(cite) and " v. " not in cite.lower():
        try:
            from pipeline.rules_index import lookup
            h = lookup(repcite or cite, limit=1)
            if h and h[0].get("body"):
                return "rule-verbatim", h[0].get("source", "")[:38]
        except Exception:
            pass
    try:
        from pipeline.drafter_grounding import _corpus_hits
        hits = _corpus_hits((cite + " " + doct)[:200], k=3)
        if hits and max((x.get("score") or 0) for x in hits) >= 0.5:
            return "corpus", (hits[0].get("source", "")[:38] if hits else "")
    except Exception:
        pass
    return None, ""

def _key(c):
    return re.sub(r"[^a-z0-9]", "", ((c.get("lever") or "") + (c.get("doctrine") or "")).lower())[:44]

def _persist_audit(case_id, validated):
    """Write the validated levers into the case audit JSON (spotted_levers[]) — the array m0 consumes via
    drafter_grounding.get_spotted_levers_grounding. Uses audit_writer's own slug so the file matches."""
    try:
        from pipeline.audit_writer import _safe_id, AUDIT_DIR
        ap = AUDIT_DIR / f"data_{_safe_id(case_id)}.json"
        data = json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else {}
        data["spotted_levers"] = [{"lever": c.get("lever"), "doctrine": c.get("doctrine"),
                                   "cite": c.get("cite"), "validation": c.get("_v"),
                                   "why": c.get("why"), "record_tie": c.get("record_tie")}
                                  for c in validated]
        ap.parent.mkdir(parents=True, exist_ok=True)
        ap.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(ap)
    except Exception:
        return ""

def spot_case(case_id: str, rounds: int = 1, write_back: bool = True, persist_audit: bool = False) -> list:
    """Discover + validate the levers on a case. Returns validated levers (each with _v/_src). Never raises."""
    cdir = ROOT / "obsidian_sync" / "cases" / _case_folder(case_id)
    tc = _target_court(case_id)
    facts = _read(cdir / "CASE_FACTS.md")
    order_txt = ""
    intake = glob.glob(str(cdir / "intake" / "*.txt")) + glob.glob(str(cdir / "*.txt"))
    def _dkt(p):
        m = re.search(r"dkt[_ ]?(\d+)", os.path.basename(p), re.I); return int(m.group(1)) if m else -1
    for p in sorted(intake, key=_dkt)[-2:]:
        order_txt += f"\n[{os.path.basename(p)}]\n" + _read(p, 4500)
    sections = [("OPERATIVE ORDER (newest)", order_txt[:5000])]
    for i, ch in enumerate(_chunks(facts, 4500)[:2]):
        sections.append((f"CASE FACTS pt{i+1}", ch))
    sections.append(("RECORD POSTURE (facts, not law)",
        "Plaintiff proceeds without retained counsel and in his own name. The operative memorandum resolved "
        "the motions without individually addressing each argument and each item of record evidence the "
        "plaintiff raised. The action reached this court by removal and proceeded to a final judgment."))

    seen, validated = set(), []
    def _ingest(cands):
        n = 0
        for c in cands:
            k = _key(c)
            if not k or k in seen:
                continue
            seen.add(k)
            vtag, src = _validate(c, tc)
            if vtag:
                c["_v"], c["_src"] = vtag, src
                validated.append(c); n += 1
        return n

    for label, text in sections:
        if not text.strip():
            continue
        avoid = ", ".join(sorted({c["lever"] for c in validated if c.get("lever")}))[:1000]
        ask = f"SECTION [{label}]:\n{text}" + (f"\n\nAlready surfaced (do NOT repeat): {avoid}" if avoid else "") + "\nJSON array only."
        _ingest(_llm(ask, _SYS))

    # DEEPEN — push past mainstream, seeded by the corpus GRAPH's structural neighbors (deterministic)
    for _ in range(max(rounds, 1)):
        base = list(validated)
        for c in base[:8]:
            lever, cite = c.get("lever", ""), c.get("cite", "")
            gctx = ""
            try:
                from pipeline import corpus_graph as cg
                neigh = []
                for _typ, cc in cg.authorities_in(lever + " " + (c.get("doctrine") or "")):
                    # MULTI-HOP document-level co-citation: the doctrine NEIGHBORHOOD (dense cross-references
                    # + their cross-references), canon2-merged + frequency-filtered — full-law-application
                    # connectivity that pushes m0 past the mainstream doctrine to the connected ones.
                    neigh += [x for x, _w in cg.co_cited_deep(cc, hops=2, k=6)]
                if neigh:
                    gctx = "Authorities the related law cross-references (doctrine neighborhood): " + ", ".join(sorted(set(neigh))[:16])
            except Exception:
                gctx = ""
            try:
                from pipeline.connected_notes import graph_context
                gctx = (gctx + "\n" + (graph_context((lever + " " + (c.get('doctrine') or ''))[:280], k=3, max_chars=900) or "")).strip()
            except Exception:
                pass
            avoid = ", ".join(sorted({v["lever"] for v in validated if v.get("lever")}))[:900]
            ask = (f"Court: {tc}. Another lawyer raised: \"{lever}\" citing {cite or '(none)'}.\n\n"
                   f"{gctx or '(no graph context)'}\n\nGo deeper; flag the cite if non-binding. "
                   f"Already surfaced (do NOT repeat): {avoid}\nJSON array only.")
            _ingest(_llm(ask, _DEEP_SYS, max_tokens=3800))

    # FLYWHEEL — write every validated lever back (corpus-tagged; relink deferred to the ingest cron)
    if write_back:
        try:
            from pipeline.connected_notes import record_learning
            for c in validated:
                cite = (c.get("cite") or "").strip()
                if not cite or cite.upper() == "VERIFY":
                    continue
                tag = "official" if c["_v"].startswith(("case-BINDING", "rule-verbatim")) else "corpus"
                note = f"{c.get('lever','')} — {c.get('doctrine','')}. Authority: {cite} [{c['_v']}]."
                try:
                    record_learning(note.strip(), tags=[tag, "issue-spotter"], source="issue-spotter",
                                    cite=cite, relink_now=False)
                except Exception:
                    pass
        except Exception:
            pass
    if persist_audit:
        _persist_audit(case_id, validated)
    return validated

if __name__ == "__main__":
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    levers = spot_case(cid, rounds=1, write_back=False)
    print(f"\n{len(levers)} validated levers for {cid}:")
    for c in levers:
        print(f"  [{c['_v']:16}] {c.get('lever','?')[:66]}  | {c.get('cite','')[:28]}")
