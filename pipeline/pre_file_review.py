"""pre_file_review — the PRE-FILE auto-scan that fires after the drafter produces a motion.

Two jobs, both additive (never changes the draft):
  1. Scan OUR draft for term-of-art misuse (term_of_art_scanner) — fix before filing.
  2. Surface the available ENFORCEMENT TARGETS for the case (the divergence ledgers, Stage 11/11.5) —
     so the draft can press the statute/reg/manual/court-rule divergences as relief.

Writes a `*_PREFILE_REVIEW.md` sidecar next to the draft + a copy under output/pre_file_reviews/.
Non-destructive; wrapped in try/except by callers so a failure never blocks a draft.

CLI:
  python -m pipeline.pre_file_review --file draft.md --slug fed_26-cv-00456
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
LEDGERS = ROOT / "output" / "ledgers"
OUT = ROOT / "output" / "pre_file_reviews"


def _slugify(case_id: str) -> str:
    if not case_id:
        return ""
    if case_id.startswith("fed:"):
        parts = case_id.split(":")
        return f"fed_{parts[2]}" if len(parts) > 2 else "fed_" + parts[-1]
    if case_id.startswith("phila_ccp:"):
        return "phila_ccp_" + case_id.split(":", 1)[1]
    return case_id.replace(":", "_").replace("/", "_")


def _enforcement_targets(slug: str) -> dict:
    out = {"agency": [], "court": []}
    for kind in ("agency", "court"):
        p = LEDGERS / f"ledger_{kind}_{slug}.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                for r in d.get("rows", [])[:30]:
                    out[kind].append({"field": (r.get("field_did") or "")[:120],
                                      "vehicle": str(r.get("vehicle", ""))[:160]})
            except Exception:
                pass
    return out


def review(draft_text: str, case_id: str = "", draft_path: str | None = None) -> dict:
    from pipeline.term_of_art_scanner import scan_text
    slug = _slugify(case_id) if case_id else (Path(draft_path).stem.split("_")[0] if draft_path else "")
    toa = scan_text(draft_text or "")
    # de-dup term-of-art by (term, surrounding context)
    seen, uniq = set(), []
    for f in toa:
        k = (f["term"], f["context"][:40])
        if k not in seen:
            seen.add(k); uniq.append(f)
    targets = _enforcement_targets(slug)
    # Definitions schedule + elegant-variation flags (Stage 1 drafting discipline) — additive, wrapped.
    defs = {"defined_terms": 0, "variation_flags": 0, "schedule_path": ""}
    try:
        from pipeline import definitions_tagger
        defs = definitions_tagger.review(draft_text or "", slug=slug, ground=False, draft_path=draft_path)
    except Exception:
        pass
    # Tactics preempt: flag the pleading defects that invite avoidance attacks (shotgun/group/conclusory/
    # missing standing-jurisdiction-SOL/entity-id) so they get fixed before filing. Additive, wrapped.
    tac = {"flags": []}
    try:
        from pipeline import tactics_detector
        tac = tactics_detector.report(draft_text or "", mode="draft", slug=slug)
    except Exception:
        pass

    md = [f"# PRE-FILE REVIEW — {slug or '(draft)'}",
          "Additive review (the draft itself is unchanged). Fix the term-of-art flags + elegant-variation "
          "before filing; attach Schedule A; press the enforcement targets as relief.", "",
          f"## Definitions: {defs.get('defined_terms',0)} defined term(s), "
          f"{defs.get('variation_flags',0)} elegant-variation flag(s) — see Schedule A: {defs.get('schedule_path','')}",
          "",
          f"## Term-of-art to verify before filing ({len(uniq)})"]
    if not uniq:
        md.append("- none flagged")
    for f in uniq[:40]:
        md += [f"- **\"{f['match']}\"** ({f['term']})  — context: …{f['context'][:90]}…",
               f"    - proper: {f['proper']}",
               f"    - corrective: {f['corrective']}"]
    md += ["", "## Enforcement targets available (from the divergence ledgers)"]
    if not (targets["agency"] or targets["court"]):
        md.append("- (no ledger yet — run `python -m pipeline.divergence_ledger " + (slug or "<slug>") + "` [+ --court])")
    for kind in ("agency", "court"):
        for r in targets[kind]:
            md.append(f"- [{kind}] {r['field']}  →  vehicle: {r['vehicle']}")
    md += ["", f"## Pleading-defect preempts to FIX before filing ({len(tac.get('flags', []))}) "
           "— close the avoidance footholds"]
    for f in tac.get("flags", []):
        md.append(f"- **{f['tactic']}** — {f['is']}  →  {f['counter']}")

    OUT.mkdir(parents=True, exist_ok=True)
    report = "\n".join(md)
    (OUT / f"{(slug or 'draft')}_PREFILE_REVIEW.md").write_text(report, encoding="utf-8")
    if draft_path:
        try:
            side = Path(draft_path).with_name(Path(draft_path).stem.replace("_DRAFT", "") + "_PREFILE_REVIEW.md")
            side.write_text(report, encoding="utf-8")
        except Exception:
            pass
    return {"slug": slug, "term_of_art_flags": len(uniq), "enforcement_targets": targets,
            "defined_terms": defs.get("defined_terms", 0), "variation_flags": defs.get("variation_flags", 0),
            "pleading_defects": len(tac.get("flags", []))}


def fileability_check(text: str) -> list:
    """Deterministic UNFILEABLE-body detector (crack-gap G1, 2026-07-08: 3/6 recent drafts shipped
    template scaffolds, raw chain-of-thought, DRAFTER WARNINGs, or token-repeat corruption in the
    FILED body — a court needs no rule to reject those; the case is forfeited at the door).
    Pattern CLASSES only — agnostic, nothing case- or matter-specific. Shared by gate_draft and
    the pipeline healer sweep. Returns [] when clean."""
    import re as _re
    defects = []
    _checks = [
        ("template-scaffold", r"\[(?:First|Second|Third) ground|\[Insert\b|\{\{[a-z_]+\}\}|\[Where opposing authority"),
        ("drafter-warning", r"\[DRAFTER WARNING"),
        ("chain-of-thought-leak", r"\b(?:The user wants me to|I must use the definitions|I will follow|"
                                  r"in the prompt|as an AI|the stub says)\b"),
        ("markdown-heading-placeholder", r"^#{1,4}\s+[IVX]+\.\s*\[[^\]]+\]\s*$"),
        ("unverified-cite-marker", r"\[(?:UNVERIFIED CITE|CITE REVIEW|CLAIM REVIEW|CITE\?)\]"),   # L6/L3
        # L9 POSTURE-COHERENCE (self-defeating filings no other gate catches): a defendant needs no Article III
        # standing to defend/move, so demanding it OF a movant is incoherent; conceding the sued defendant is
        # NOT the cause of the injury defeats the claim; and moving to strike the party's OWN filing is absurd.
        ("posture-demands-standing-of-movant",
         r"lacks?\s+(?:Article\s+III\s+)?standing\s+to\b[^.]{0,70}(?:seek\s+dismissal|move\s+to\s+dismiss|dismiss\b|defend\b)"),
        ("posture-concedes-nontraceability",
         r"(?:injury|harm)\b[^.]{0,50}\btraceable\s+to\b[^.]{0,70}\bnot\s+(?:to\s+)?(?:the\s+)?[A-Z]\w+"),
        ("posture-attacks-own-filing",
         r"[Pp]laintiff(?:'s)?\s+(?:own\s+)?(?:ECF|filing|response|motion|pleading)\b[^.]{0,70}"
         r"(?:warrant|subject\s+to|may\s+be|should\s+be)\s+(?:strik|dismiss)"),
    ]
    for name, pat in _checks:
        m = _re.search(pat, text or "", _re.M)
        if m:
            defects.append({"defect": name, "at": (text or "")[:m.start()].count("\n") + 1,
                            "sample": m.group(0)[:80]})
    # token-repeat corruption: any 6+-word phrase recurring 6+ times
    words = (text or "").split()
    for i in range(0, max(0, len(words) - 60), 6):
        gram = " ".join(words[i:i + 6])
        if len(gram) > 20 and text.count(gram) >= 6:
            defects.append({"defect": "token-repeat-corruption", "sample": gram[:80]})
            break
    return defects


def gate_draft(draft_path: str, case_id: str = "", inject_definitions: bool = True) -> dict:
    """UNIVERSAL PRE-FILE GATE (GO step 9, 2026-07-08). Every outbound draft — whatever produced it —
    passes through the SAME discipline m0 applies, because the ecf_172 opposition shipped without a
    Schedule A and with three out-of-circuit cites (our own modeled attack vectors, live against our
    own paper):
      1. definitions/Schedule A injection when the draft lacks one (deterministic tagger; the
         12(b)(6)-ambiguity closure),
      2. adversarial pre-file review (term-of-art flags, pleading-defect preempts, enforcement targets),
      3. jurisdiction check on every cited authority (defense_simulator.attack_authorities).
    NON-BLOCKING by design: writes <draft>.prefile.json + the review sidecar and returns the verdict;
    the producer decides. It reports; it never silently rewrites legal argument."""
    import json as _json, re as _re
    p = Path(draft_path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    slug = _slugify(case_id) if case_id else ""
    verdict = {"draft": str(p), "case_id": case_id, "schedule_a": "present",
               "authority_attacks": [], "review": {}, "gated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    # 0. FILEABILITY — shared pattern classes (see fileability_check), agnostic.
    verdict["fileability_defects"] = fileability_check(text)
    verdict["fileable"] = not verdict["fileability_defects"]
    # 0b. MECHANISM PRESENCE (discretion-narrowing checks — pattern classes, jurisdiction-agnostic):
    #     absence means the court can rule adversely without confronting a duty, a locked fact, or a
    #     preserved issue. Reported, never injected — new filing prose requires human legal review.
    verdict["mechanisms"] = {
        "mandatory_anchor": bool(_re.search(r"\b(?:shall|must)\b[^.\n]{0,120}\b(?:Rule|R\.C\.P|R\.A\.P|U\.S\.C|Pa\.C\.S|C\.F\.R)", text)),
        "per_item_ruling": bool(_re.search(r"\|\s*IF\b|each\s+(?:defect|violation|question|issue)[^.\n]{0,80}\b(?:individually|separately|each)\b|rule on each", text, _re.I)),
        "preservation_waiver": bool(_re.search(r"\bwaiv(?:er|ed|es)\b|preserv(?:ed|ation)\b[^.\n]{0,60}(?:appeal|record)|failure to address", text, _re.I)),
        "standard_of_review": bool(_re.search(r"standard of review|de novo|abuse of discretion|clearly erroneous|plenary", text, _re.I)),
        "fact_lock": bool(_re.search(r"\bverif(?:ied|ication)\b|\baffidavit\b|\bsworn\b|18 Pa\.C\.S\.|28 U\.S\.C\.\s*§?\s*1746|unrebutted|Exhibit\s+[A-Z0-9]", text)),
        "burden_allocation": bool(_re.search(r"\bburden of (?:proof|persuasion|production|pleading|going forward)\b|\bbears the burden\b", text, _re.I)),
        "findings_demand": bool(_re.search(r"findings of fact|conclusions of law|specific findings|state (?:its|the) reasons", text, _re.I)),
    }
    # 1. definitions schedule — inject only if absent (same tagger m0 uses; admin types skip themselves)
    if inject_definitions and "SCHEDULE A" not in text.upper():
        try:
            from pipeline import definitions_tagger
            injected = definitions_tagger.inject_into_draft(text, slug=slug)
            if injected and injected != text:
                p.write_text(injected, encoding="utf-8")
                text = injected
                verdict["schedule_a"] = "injected"
            else:
                verdict["schedule_a"] = "absent (tagger produced no schedule)"
        except Exception as e:
            verdict["schedule_a"] = f"absent (inject failed: {e})"
    # 2. adversarial review (writes the sidecar report)
    try:
        verdict["review"] = review(text, case_id=case_id, draft_path=str(p))
    except Exception as e:
        verdict["review"] = {"error": str(e)}
    # 3. authority jurisdiction check — every " v. " cite line goes through the defense simulator
    try:
        from pipeline.defense_simulator import attack_authorities
        cites = list({m.group(0).strip() for m in _re.finditer(
            r"[A-Z][A-Za-z.,'’& -]{2,60}\sv\.?\s[A-Z][A-Za-z.,'’& -]{2,60}(?:,\s*[^()\n]{0,60})?(?:\([^)]{2,40}\))?",
            text)})
        tj = "EDPA" if (case_id or "").startswith("fed") else ""
        atks = attack_authorities(cites, target_jurisdiction=tj)
        verdict["authority_attacks"] = [
            {"target": getattr(a, "target", ""), "attack": getattr(a, "attack", ""),
             "strength": getattr(a, "strength", "")} for a in (atks or [])]
    except Exception as e:
        verdict["authority_attacks"] = [{"error": str(e)}]
    # 4. G2: if the draft demands no findings (bare GRANTED/DENIED order), stage a [PROPOSED] FINDINGS
    #    block derived 1:1 from the audit rows as a REVIEW-FIRST sibling — never injected into the body.
    if case_id and not verdict["mechanisms"].get("findings_demand"):
        try:
            from pipeline.scorecard_if_then import build_proposed_findings
            blk = build_proposed_findings(case_id, is_state=not case_id.startswith("fed"))
            if blk:
                sib = p.with_name(p.stem + ".PROPOSED_FINDINGS.md")
                sib.write_text(blk, encoding="utf-8")
                verdict["proposed_findings"] = str(sib)
        except Exception as e:
            verdict["proposed_findings"] = f"(failed: {e})"
    # 5. CROSS-STATUTE definition-import check (2026-07-09): flag terms whose closed 'means' definition is
    #    homed in one statute but used with another (ours to fix, or the opponent's to expose). Graph-driven.
    try:
        from pipeline.cross_statute_guard import check_text
        xs = check_text(text)
        verdict["cross_statute_flags"] = [{"term": f["term"], "note": f["note"]} for f in xs]
    except Exception as e:
        verdict["cross_statute_flags"] = [{"error": str(e)}]
    try:
        p.with_suffix(p.suffix + ".prefile.json").write_text(
            _json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return verdict


if __name__ == "__main__":
    fp = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
    slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else ""
    if "--gate" in sys.argv and fp:
        import json as _json
        print(_json.dumps(gate_draft(fp, case_id=slug), indent=2)[:1500])
        sys.exit(0)
    text = Path(fp).read_text(encoding="utf-8", errors="ignore") if fp else "Defendant says the complaint is frivolous and fails to state a claim; the Holder may foreclose."
    r = review(text, case_id=slug, draft_path=fp)
    print(f"pre-file review: {r['term_of_art_flags']} term-of-art flags; "
          f"targets agency={len(r['enforcement_targets']['agency'])} court={len(r['enforcement_targets']['court'])}")
    print(f"  -> {OUT / ((r['slug'] or 'draft')+'_PREFILE_REVIEW.md')}")
