"""pipeline/jurisdiction_check.py — AGNOSTIC subject-matter-jurisdiction / removal-propriety detector.

For ANY removed case, derives the jurisdictional lever the other detectors miss and writes it into the
audit JSON (additive key `jurisdictional_defects`, read by the drafter). Data-driven from the case
registry (R2: no hardcoded case data). Stands DOWN when a federal claim is actually pleaded on the face
(so the FCRA case does not get a false SMJ finding — the regression guard).

Chain: removed?  ->  federal claim on the face?  ->  (if not) removal on FQ improper  ->  diversity complete?
       ->  (if not) no SMJ -> judgment void + remand.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

# Resolve the repo root by testing for the registry FILE under each candidate (cwd-independent).
# On WSL /mnt/c wins; on native Windows C:\ wins. (A bare Path(r"C:\...").exists() is cwd-relative
# on Linux and reads the registry as empty — the bug the sandbox caught.)
_ROOT = next((c for c in (_eroot(), _eroot())
              if (c / "cases_index" / "case_registry.json").exists()), _eroot())
_REGISTRY = _ROOT / "cases_index" / "case_registry.json"
_AUDIT_DIR = _ROOT / "output" / "audit"
_CASES = _ROOT / "obsidian_sync" / "cases"

# case_type / engine-fact tokens whose PLEADED cause arises under federal law
_FEDERAL_TOKENS = ("fcra", "fdcpa", "tila", "truth in lending", "fair credit", "fair debt",
                   "1681", "1692", "1640", "federal question", "15 u.s.c", "12 u.s.c")

def _reg():
    try: return json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except Exception: return {}

def _entry(slug):
    reg = _reg(); norm = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    for cid, e in reg.items():
        for cand in (e.get("slug"), cid, cid.replace(":", "_")):
            if cand and re.sub(r"[^a-z0-9]", "", cand.lower()) == norm:
                return cid, e
        if norm and norm.endswith(re.sub(r"[^a-z0-9]", "", cid.split(":")[-1].lower())):
            return cid, e
    return None, {}

def _slugfs(cid, e):
    return e.get("slug") or ("fed_" + cid.split(":")[-1]) if cid else None

def _case_facts_text(cid, e):
    fs = _slugfs(cid, e)
    for name in (fs, cid and cid.replace(":", "_")):
        if not name: continue
        p = _CASES / name / "CASE_FACTS.md"
        if p.exists():
            try: return p.read_text(encoding="utf-8", errors="ignore")
            except Exception: pass
    return ""

def run(court_type: str = "federal", claim_arises_under: str = "federal_law",
        defendant_in_forum_state: bool = True, was_removed: bool = False,
        diversity: bool = False, **kwargs) -> dict:
    """RESTORED 2026-08-02 (the 'Four gates failed' bug fix). Evaluate jurisdiction from the SUPPLIED INPUTS
    — the interface four_gates.gate_jurisdiction and reasoning_orchestrator.build_strategy call. Distinct
    from analyze(slug), which reads a case from the registry. Deterministic; every branch carries its cite.
    Returns {overall, subject_matter, personal, removal, blocking_issues, *_authority}."""
    blocking, sm_auth, pj_auth, rm_auth = [], [], [], []
    federal_q = (claim_arises_under or "").lower() in ("federal_law", "federal", "federal_question")
    if (court_type or "").lower() == "federal":
        if federal_q:
            subject_matter = "OK"; sm_auth.append("28 U.S.C. § 1331 (federal question)")
        elif diversity:
            subject_matter = "OK"; sm_auth.append("28 U.S.C. § 1332 (diversity of citizenship)")
        else:
            subject_matter = "DEFECTIVE"; sm_auth.append("28 U.S.C. §§ 1331, 1332")
            blocking.append("No federal question and no complete diversity — the federal court lacks "
                            "subject-matter jurisdiction (28 U.S.C. §§ 1331, 1332)")
    else:
        subject_matter = "OK"  # state court of general jurisdiction
    if defendant_in_forum_state:
        personal = "OK"
    else:
        personal = "UNCLEAR"
        blocking.append("Personal jurisdiction over a non-forum defendant must be established "
                        "(International Shoe Co. v. Washington, 326 U.S. 310 (1945))")
    pj_auth.append("International Shoe Co. v. Washington, 326 U.S. 310 (1945)")
    if was_removed:
        rm_auth.append("28 U.S.C. §§ 1441, 1446, 1447")
        if (court_type or "").lower() == "federal" and not (federal_q or diversity):
            removal = "DEFECTIVE"
            blocking.append("Removal without a federal question or complete diversity is defective; "
                            "remand is required (28 U.S.C. § 1447(c))")
        else:
            removal = "OK"
    else:
        removal = "N/A"
    return {
        "overall": "DEFECTIVE" if blocking else "OK",
        "subject_matter": subject_matter, "personal": personal, "removal": removal,
        "blocking_issues": blocking,
        "subject_matter_authority": sm_auth, "personal_authority": pj_auth, "removal_authority": rm_auth,
    }


def analyze(slug: str) -> dict:
    cid, e = _entry(slug)
    if not e:
        return {"slug": slug, "status": "no_registry_entry", "jurisdictional_defects": []}
    ctype = (e.get("case_type") or "").lower()
    facts_blob = (ctype + " " + " ".join(e.get("engine_facts") or [])).lower()
    cf = _case_facts_text(cid, e)
    cf_l = cf.lower()

    removed = bool(e.get("state_origin_docket")) or bool(
        re.search(r"notice of removal|removed to|removed under|28[:\s]*1441|§\s*1441", cf_l))
    federal_cause = any(tok in ctype for tok in _FEDERAL_TOKENS)

    # LLC/partnership defendants (member-citizenship must be proven by remover)
    caption = (e.get("caption") or "") + " " + cf
    llc_defs = sorted(set(re.findall(r"([A-Z][A-Za-z.&' ]{2,40},?\s*(?:LLC|L\.L\.C\.|LP|L\.P\.))", caption)))

    defects = []
    if removed and not federal_cause:
        defects.append({
            "type": "REMOVAL_NO_FEDERAL_QUESTION",
            "summary": f"Pleaded cause is '{ctype or 'state-law'}' (state law). No federal claim appears on the "
                       f"face of the well-pleaded complaint; a federal issue or defense in the facts does not "
                       f"create federal-question jurisdiction.",
            "authority": ["Tennessee v. Union & Planters' Bank, 152 U.S. 454",
                          "Franchise Tax Board v. Construction Laborers, 463 U.S. 1",
                          "28 U.S.C. § 1441; Shamrock Oil & Gas v. Sheets, 313 U.S. 100"],
        })
        defects.append({
            "type": "DIVERSITY_UNVERIFIED",
            "summary": "Removal can survive only on complete diversity. The removing party bears the burden; for "
                       + (", ".join(llc_defs) if llc_defs else "any LLC/partnership defendant")
                       + " citizenship is that of every member, which must be traced and shown non-forum.",
            "authority": ["28 U.S.C. § 1332", "Carden v. Arkoma Associates, 494 U.S. 185",
                          "Zambelli Fireworks v. Wood, 592 F.3d 412"],
            "verify": "Confirm the removal basis on the Notice of Removal and trace each LLC defendant's member chain.",
        })
        defects.append({
            "type": "SMJ_VOID_IF_NO_DIVERSITY",
            "summary": "If there is no federal question and diversity is not complete, the court lacked subject-"
                       "matter jurisdiction; any judgment is void and the case must be remanded. A court cannot "
                       "create jurisdiction through its own mistakes.",
            "authority": ["Hain Celestial Group v. Palmquist (2026)",
                          "Royal Canin U.S.A. v. Wullschleger (2025)",
                          "28 U.S.C. § 1447(c); FRCP 12(h)(3)"],
        })
    return {"slug": slug, "canonical_id": cid, "case_type": ctype, "removed": removed,
            "federal_claim_on_face": federal_cause, "llc_defendants": llc_defs,
            "jurisdictional_defects": defects}

def write_audit(slug: str) -> dict:
    """Additive: sets audit['jurisdictional_defects'] + a small summary. Never touches existing arrays."""
    res = analyze(slug)
    cid = res.get("canonical_id")
    fs = None
    if cid:
        _, e = _entry(slug); fs = _slugfs(cid, e)
    # audit file names use fed_2_25-cv-... form
    cand_names = [f"data_{(fs or '').replace('fed_25','fed_2_25')}.json",
                  f"data_{(cid or '').replace(':','_')}.json"]
    for nm in cand_names:
        p = _AUDIT_DIR / nm
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                d["jurisdictional_defects"] = res["jurisdictional_defects"]
                d["_jx_summary"] = {k: res[k] for k in ("removed", "federal_claim_on_face", "llc_defendants")}
                p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                res["written_to"] = str(p)
                return res
            except Exception as ex:
                res["error"] = str(ex); return res
    res["written_to"] = None
    return res

if __name__ == "__main__":
    import sys
    print(json.dumps(analyze(sys.argv[1] if len(sys.argv) > 1 else "fed_26-cv-00456"), indent=2))
