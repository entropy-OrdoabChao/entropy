"""new_case — register a case and create its vault folder, deterministically (no LLM, no hardcoded names).
Used by the desk's "Load a case I already have" and by the story wizard once the claim is routed. The person's name comes from
.env (USER_NAME); the caption is built from the parties given. Idempotent: an existing canonical id is updated, not duplicated."""
import json, re, sys, time
from pathlib import Path
from pipeline import paths as _P
def _env(key, default=""):
    try:
        for line in _P.ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="): return line.split("=", 1)[1].strip().strip('"')
    except Exception: pass
    return default
def slug_for(court_code: str, docket: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", f"{court_code}_{docket}".lower()).strip("_")
def register(court_code: str, docket: str, defendants: list, caption: str = None, judge: str = "", case_type: str = "general",
             status: str = "active", plaintiff: str = None, court_name: str = "", docket_aliases: list = None) -> dict:
    plaintiff = plaintiff or _env("USER_NAME", "PLAINTIFF")
    cid = f"{court_code.lower()}:{docket}"; slug = slug_for(court_code, docket)
    caption = caption or f"{plaintiff.upper()} v. {', '.join(d.upper() for d in defendants[:3])}{' ET AL' if len(defendants) > 3 else ''}"
    folder = _P.CASES / slug
    entry = {"canonical_id": cid, "case_court": court_code, "court_name": court_name, "caption": caption, "filed": time.strftime("%Y-%m-%d"), "status": status,
             "case_type": case_type, "judge": judge, "docket_aliases": sorted(set([docket] + (docket_aliases or []))), "federal_cross_ref": None, "state_origin_docket": None,
             "parties": [re.sub(r"\W+", "_", plaintiff.lower())] + [re.sub(r"\W+", "_", d.lower()) for d in defendants], "related_cases": [], "notes": [f"registered {time.strftime('%Y-%m-%d %H:%M')} by pipeline.new_case"],
             "engine_facts": [], "factual_application": {}, "counsel_of_record": [], "claim_priority": [], "slug": slug, "folder_path": str(folder)}
    _P.CASES_INDEX.mkdir(parents=True, exist_ok=True)
    reg = json.loads(_P.REGISTRY.read_text(encoding="utf-8")) if _P.REGISTRY.exists() else {}
    reg[cid] = {**reg.get(cid, {}), **entry}
    _P.REGISTRY.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    for sub in ("intake", "intake/_ocr", "drafts", "filings", "exhibits", "emails", "scorecards", "notes"): (folder / sub).mkdir(parents=True, exist_ok=True)
    cf = folder / "CASE_FACTS.md"
    if not cf.exists():
        cf.write_text(f"---\ntype: case_facts\ncase_id: {cid}\ncase_slug: {slug}\nupdated: {time.strftime('%Y-%m-%d')}\nstatus: needs_first_analysis\ntags:\n  - case_facts\n  - case/{slug}\n---\n\n# CASE FACTS — {cid} ({caption})\n\n"
                      f"## BASIC INFO\n- **Case ID:** {cid}\n- **Caption:** {caption}\n- **Court:** {court_name or court_code}\n- **Judge:** {judge or 'unknown'}\n- **Status:** {status}\n\n"
                      "## FIRST ANALYSIS PENDING\nDrop the case papers into the drop folder (or this case's intake folder). The desk sorts, reads and analyzes them; this file is then rewritten by the analysis, section by section, from the documents and the law library. Nothing here is a legal conclusion yet.\n", encoding="utf-8")
    return entry
def main():
    import argparse
    ap = argparse.ArgumentParser(description="register a case and create its vault folder")
    ap.add_argument("--court", required=True, help="court code, e.g. fed_edpa, pa_cp_phila"); ap.add_argument("--docket", required=True); ap.add_argument("--defendant", action="append", default=[])
    ap.add_argument("--caption"); ap.add_argument("--judge", default=""); ap.add_argument("--type", default="general"); ap.add_argument("--court-name", default="")
    a = ap.parse_args(); e = register(a.court, a.docket, a.defendant, a.caption, a.judge, a.type, court_name=a.court_name); print(json.dumps({k: e[k] for k in ("canonical_id", "slug", "caption", "folder_path")}, indent=1))
if __name__ == "__main__": main()
