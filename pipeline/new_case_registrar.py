#!/usr/bin/env python3
"""THE BRIDGE (the missing organ): turn a viable email-found claim into a REGISTERED VAULT CASE so the
existing machinery lights up — creates obsidian_sync/cases/<slug>/ (+ intake/ with the evidence emails),
writes the INITIAL CASE_FACTS.md (case_facts_refresh only UPDATES; nothing created it before), and adds an
add-only entry to cases_index/case_registry.json. Data-driven from the claim (R2: no hardcoded case data).
Idempotent; dry_run returns the artifacts without writing. Once the folder + CASE_FACTS exist,
case_facts_refresh and case_readiness work on it natively with no further wiring.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, shutil
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if p.exists()), _eroot())
CASES = ROOT / "obsidian_sync" / "cases"
REGISTRY = ROOT / "cases_index" / "case_registry.json"
EVIDENCE = ROOT / "obsidian_sync" / "unmatched_emails"


def _company(domain: str) -> str:
    base = re.sub(r"^(mail|email|updates?|offers?|e|notifications?)\.", "", (domain or "").lower())
    return base.split(".")[0].replace("-", " ").title() if base else (domain or "Defendant")


def _slug(claim: dict) -> str:
    d = re.sub(r"[^a-z0-9]+", "_", (claim.get("defendant_domain") or "party").lower()).strip("_")
    return f"{d}_{claim.get('claim_type', 'claim')}"[:48]


def _case_court(court_key: str) -> str:
    return {"phila_ccp": "PA_CP_PHILA", "fed": "EDPA"}.get(court_key or "", court_key or "")


def _case_facts_md(cid: str, claim: dict, company: str, facts: list) -> str:
    L = [f"# CASE FACTS - {cid}", "",
         "## BASIC INFO",
         f"- **Defendant:** {company}  (`{claim.get('defendant_domain','')}`)",
         f"- **Claim type:** {claim.get('claim_type','')}",
         f"- **Category:** {claim.get('violation_category','')}",
         f"- **Forum (routed):** {claim.get('court','')} / {claim.get('venue','')}",
         f"- **Status:** pre-suit / administrative (new email-sourced claim)",
         f"- **Source emails:** {', '.join(claim.get('evidence_emails', []) or []) or '(see intake/)'}",
         "",
         "## FACTS (from the record — verbatim quotes only)"]
    if facts:
        for f in facts:
            q = f.get("quote") if isinstance(f, dict) else str(f)
            src = f.get("source", "") if isinstance(f, dict) else ""
            L.append(f"- \"{ ' '.join(str(q).split())[:300] }\"" + (f"  [{src}]" if src else ""))
    else:
        L.append("- (Adverse action pending extraction — evidence emails staged in intake/.)")
    L += ["", "## STRATEGY NOTES",
          f"- New consumer claim promoted from email scan. Route: {claim.get('court','')}."
          " Notices-first (party + regulator) then m0 draft per the improved drafter.",
          "- Required forms: " + (", ".join(claim.get("required_forms", []) or []) or "n/a"), ""]
    return "\n".join(L)


def register_new_case(claim: dict, facts: list = None, dry_run: bool = False) -> dict:
    """Create the vault case + initial CASE_FACTS + registry entry for a queued claim. Idempotent + add-only."""
    slug = _slug(claim)
    company = _company(claim.get("defendant_domain"))
    court_key = claim.get("court") or "fed"
    cid = f"consumer:{slug}"
    folder = CASES / slug
    entry = {
        "canonical_id": cid, "case_court": _case_court(court_key),
        "caption": f"FILER v. {company.upper()}", "case_type": claim.get("claim_type", ""),
        "status": "pre_suit", "parties": ["filer", slug.split("_")[0]],
        "engine_facts": [claim.get("violation_category", ""), claim.get("claim_type", "")],
        "slug": slug, "folder_path": str(folder), "source": "new_case_registrar",
    }
    cf_text = _case_facts_md(cid, claim, company, facts or [])
    if dry_run:
        return {"dry_run": True, "canonical_id": cid, "slug": slug, "folder": str(folder),
                "registry_entry": entry, "case_facts_preview": cf_text[:600]}
    # ---- live writes (add-only, idempotent) ----
    (folder / "intake").mkdir(parents=True, exist_ok=True)
    for name in (claim.get("evidence_emails") or []):
        for cand in EVIDENCE.glob(name + "*"):
            if cand.suffix == ".md":
                try:
                    shutil.copy2(cand, folder / "intake" / cand.name)
                except Exception:
                    pass
    cf = folder / "CASE_FACTS.md"
    if not cf.exists():                       # never clobber an existing (case_facts_refresh owns updates)
        cf.write_text(cf_text, encoding="utf-8")
    reg = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {}
    added = False
    if cid not in reg:
        reg[cid] = entry
        REGISTRY.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        added = True
    return {"canonical_id": cid, "slug": slug, "folder": str(folder),
            "case_facts_written": cf.exists(), "registry_added": added}


if __name__ == "__main__":
    import sys
    q = json.loads((ROOT / "output" / "new_claims_queue.json").read_text(encoding="utf-8"))
    claims = [c for c in (q.get("promoted") or []) if c.get("status") == "pending_m0_draft"]
    for c in claims[:1]:
        print(json.dumps(register_new_case(c, dry_run=("--live" not in sys.argv)), indent=2)[:900])
