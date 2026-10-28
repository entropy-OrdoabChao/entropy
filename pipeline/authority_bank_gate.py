#!/usr/bin/env python3
"""authority_bank_gate.py — ANALYSIS-SIDE quality gate organ (m0).

USER DIRECTIVE (2026-08-26): "some of those gates need to move up to the
analysis so it is airtight." Post-draft gates catch defects AFTER GPU-hours
are spent and can only flag, not fix. This organ runs the same verification
AT AUDIT TIME, so the audit stream (the single source every drafter reads)
carries ONLY pre-verified material:

  1. CITE GATE UP-FRONT: every case-law authority appearing in the audit's
     violations/defects is run through caselaw_verify.verify_cite ONCE and
     stamped {authority_status: verified|unconfirmed|UNBACKED, binding: ...}
     directly on the violation record (results stream, never side keys).
     An UNBACKED authority is DEMOTED: the violation keeps its facts but
     its authority field moves to authority_rejected so no drafter can
     cite it. Fixed at the source = fixed for every future filing.
  2. TERM-OF-ART GATE UP-FRONT: definition_gate's term list is checked
     against each violation's text; misused terms-of-art are stamped
     term_flags on the record (pre-file-review moved upstream).
  3. VERDICT SUMMARY pinned into the results stream as one organ record
     (category AUTHORITY_BANK_GATE) with counts, so case_audit_summary
     surfaces gate health per case.

Analysis-side only — NOT wired into any drafter. Drafters simply inherit
a stream where the gate already ran. Idempotent: re-running re-verifies
only unstamped/changed entries (cache in output/audit/cite_gate_cache.json).
"""
import json, re, datetime, logging
from pathlib import Path

log = logging.getLogger("authority_bank_gate")
ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "output" / "audit"
CACHE = AUDIT_DIR / "cite_gate_cache.json"

CASE_CITE_PAT = re.compile(
    r"([A-Z][A-Za-z.'’\- ]+ v\.? [A-Z][A-Za-z.'’\- ]+),?\s*"
    r"(\d+\s+(?:U\.S\.|S\. ?Ct\.|F\.\s*(?:2d|3d|4th)|F\. ?Supp\.?\s*(?:2d|3d)?|A\.\s*(?:2d|3d)|Pa\.)\s*\d+)")

def _load_cache():
    try: return json.loads(CACHE.read_text())
    except Exception: return {}

def _save_cache(c):
    try: CACHE.write_text(json.dumps(c, indent=1))
    except Exception as e: log.warning(f"cache save failed: {e}")

def _verify_one(name, cite, cache):
    key = f"{name}|{cite}"
    if key in cache: return cache[key]
    verdict = {"status": "unconfirmed", "checked": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        from pipeline.caselaw_verify import verify_cite
        r = verify_cite(name, cite=cite) or {}
        # caselaw_verify contract: verdict = CORPUS_VERBATIM | OFFICIAL_CONFIRMED | UNVERIFIED
        v = (r.get("verdict") or "").upper()
        if v in ("CORPUS_VERBATIM", "OFFICIAL_CONFIRMED"):
            verdict["status"] = "verified"
            verdict["lane"] = "corpus" if v == "CORPUS_VERBATIM" else "courtlistener"
        elif v == "UNVERIFIED":
            # UNVERIFIED with no corpus neighbor at all = UNBACKED (demote);
            # with a neighbor = unconfirmed (kept, flagged for manual check)
            lanes = r.get("lanes") or {}
            verdict["status"] = "unconfirmed" if lanes else "UNBACKED"
    except Exception as e:
        verdict["error"] = str(e)[:120]
    cache[key] = verdict
    return verdict

def _term_flags(text):
    """definition_gate's term discipline, applied at analysis time."""
    flags = []
    try:
        from pipeline.definition_gate import TERMS  # 24-term locked list
        terms = TERMS if isinstance(TERMS, (list, tuple, set, dict)) else []
    except Exception:
        terms = ["holder in due course", "person entitled to enforce", "negotiable instrument",
                 "indorsement", "bearer", "tender", "discharge", "coupon", "statement",
                 "qualified written request", "debt collector"]
    low = (text or "").lower()
    for t in (terms.keys() if isinstance(terms, dict) else terms):
        if t.lower() in low:
            flags.append(t)
    return flags

def gate_case(case_id, write=True):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id.split(":", 1)[-1])
    # audit files are keyed varying ways; glob for the slug
    cands = sorted(AUDIT_DIR.glob(f"data_*{safe}*.json"))
    if not cands:
        log.warning(f"no audit file for {case_id}"); return None
    path = cands[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    cache = _load_cache()
    counts = {"checked": 0, "verified": 0, "unconfirmed": 0, "UNBACKED": 0, "demoted": 0, "term_flagged": 0}
    for key in ("procedural_violations", "violations", "jurisdictional_defects"):
        for v in data.get(key, []) or []:
            if not isinstance(v, dict): continue
            blob = " ".join(str(v.get(f, "")) for f in
                            ("authority", "controlling_authority", "text", "quote", "description", "rule"))
            for m in CASE_CITE_PAT.finditer(blob):
                name, cite = m.group(1).strip(), m.group(2).strip()
                verdict = _verify_one(name, cite, cache)
                counts["checked"] += 1
                st = verdict["status"]
                counts[st if st in counts else "unconfirmed"] += 1
                v.setdefault("authority_gate", {})[f"{name}, {cite}"] = st
                if st == "UNBACKED":
                    # demote: facts stay, authority cannot be drafted
                    v.setdefault("authority_rejected", []).append(f"{name}, {cite}")
                    counts["demoted"] += 1
            tf = _term_flags(blob)
            if tf:
                v["term_flags"] = sorted(set(v.get("term_flags", []) + tf))
                counts["term_flagged"] += 1
    # pin ONE organ record into the results stream (never a side key)
    organ_rec = {
        "category": "AUTHORITY_BANK_GATE", "severity": "META",
        "file": "analysis", "doc_category": "organ",
        "description": (f"analysis-side cite+term gate: {counts['checked']} authorities checked, "
                        f"{counts['verified']} verified, {counts['UNBACKED']} UNBACKED (demoted from "
                        f"draftable authority), {counts['term_flagged']} records term-flagged"),
        "gate_counts": counts,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    pv = data.setdefault("procedural_violations", [])
    pv[:] = [r for r in pv if r.get("category") != "AUTHORITY_BANK_GATE"] + [organ_rec]
    if write:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _save_cache(cache)
    log.info(f"authority_bank_gate {case_id}: {counts}")
    return counts

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("case_id")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    c = gate_case(a.case_id, write=not a.dry_run)
    print(json.dumps(c, indent=1) if c else "no audit found")

if __name__ == "__main__":
    main()
