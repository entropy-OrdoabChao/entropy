"""intake_flow — the out-of-the-box chain: drop/ -> route to the case -> read (text/OCR) -> timeline -> case state.
Deterministic and local; the model-driven steps (facts refresh, analysis, drafting) run afterwards when a model is configured.
    python -m pipeline.intake_flow            # dry run (shows what would move)
    python -m pipeline.intake_flow --apply    # move confident matches and process
Prints a summary the desk shows under Inbox and Settings > Files and vault."""
import json, sys, time
from pathlib import Path
from pipeline import paths as _P
def run(apply: bool = False) -> dict:
    from pipeline import intake_router, ocr_intake, timeline_build, case_state
    t0 = time.time(); summary = {"home": str(_P.HOME), "apply": apply, "steps": []}
    drop_before = [p.name for p in _P.DROP.iterdir() if p.is_file()] if _P.DROP.exists() else []
    summary["drop_files"] = len(drop_before)
    intake_router.route(apply=apply); summary["steps"].append("route")
    drop_after = [p.name for p in _P.DROP.iterdir() if p.is_file()] if _P.DROP.exists() else []
    summary["routed"] = len(drop_before) - len(drop_after); summary["left_in_drop"] = drop_after
    cand = _P.OUTPUT / "new_claims_intake" / "candidates.json"
    summary["new_claim_candidates"] = len(json.loads(cand.read_text(encoding="utf-8")).get("candidates", [])) if cand.exists() else 0
    if apply:
        cases = sorted(d for d in _P.CASES.iterdir() if d.is_dir() and not d.name.startswith(("_", "-"))) if _P.CASES.exists() else []
        summary["cases"] = {}
        for c in cases:
            idx = ocr_intake.process_case(c)
            tl = timeline_build.build(c)
            summary["cases"][c.name] = {"documents_read": idx["document_count"] if idx else 0, "timeline_entries": len(tl["entries"]), "timeline_added": tl["added_last_run"]}
        summary["steps"] += ["read", "timeline"]
        case_state.main(); summary["steps"].append("state")
    summary["seconds"] = round(time.time() - t0, 1)
    _P.LOGS.mkdir(parents=True, exist_ok=True); (_P.LOGS / "intake_flow_last.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
if __name__ == "__main__":
    s = run(apply="--apply" in sys.argv); print(json.dumps(s, indent=1))
