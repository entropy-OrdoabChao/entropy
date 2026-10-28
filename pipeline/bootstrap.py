"""bootstrap — create the Entropy data layout on first run (idempotent). Nothing is downloaded, nothing phones home.
    python -m pipeline.bootstrap [--home PATH]"""
import json, os, sys, time
from pathlib import Path
def main(home=None):
    if home: os.environ["ENTROPY_HOME"] = str(home)
    from pipeline import paths as _P
    import importlib; importlib.reload(_P)
    for d in (_P.DROP, _P.CASES, _P.CASES_INDEX, _P.LIBRARY, _P.OUTPUT / "audit", _P.OUTPUT / "new_claims_intake", _P.LOGS, _P.VAULT / "emails_full"): d.mkdir(parents=True, exist_ok=True)
    if not _P.REGISTRY.exists(): _P.REGISTRY.write_text("{}", encoding="utf-8")
    cm = _P.CASES_INDEX / "case_map.json"
    if not cm.exists(): cm.write_text("{}", encoding="utf-8")
    cfg = _P.HOME / "config.yaml"
    if not cfg.exists():
        import shutil; shutil.copy2(Path(__file__).resolve().parents[1] / "config.yaml", cfg)
    if not _P.ENV.exists(): _P.ENV.write_text("# Entropy settings (this computer only). Filled by Setup; edit by hand if you like.\nUSER_NAME=\nUSER_ADDRESS=\nUSER_PHONE=\nUSER_EMAIL=\nUSER_SIGNATURE=/s/\nSTATE=\nCOUNTY=\nMODEL_URL=http://127.0.0.1:8801/v1\n", encoding="utf-8")
    seed = Path(__file__).resolve().parents[1] / "vault_seed"
    if seed.exists():
        import shutil
        for item in seed.iterdir():
            dst = _P.VAULT / item.name
            if not dst.exists(): (shutil.copytree if item.is_dir() else shutil.copy2)(item, dst)
    for name, body in (("memory.md", "# Who\n\n(filled by Setup step 1)\n\n# Cases\n\n(one line per case, written by the desk)\n"), ("AGENTS.md", "# The context rule\n\nRead memory.md, then the case's CASE_FACTS.md and CASE_STATE.md, then the library. Cite [corpus] / [official] / [doc]; say NOT IN CORPUS when the library has nothing.\n")):
        p = _P.VAULT / name
        if not p.exists(): p.write_text(body, encoding="utf-8")
    (_P.DROP / "README.txt").write_text("Put anything about your case here: court papers, letters, screenshots, PDFs, photos.\nThe desk sorts them into the right case, reads them, and shows them under Inbox.\nNothing is sent anywhere.\n", encoding="utf-8")
    info = {"home": str(_P.HOME), "drop": str(_P.DROP), "vault": str(_P.VAULT), "registry": str(_P.REGISTRY), "created": time.strftime("%Y-%m-%d %H:%M")}
    print(json.dumps(info, indent=1)); return info
if __name__ == "__main__":
    a = sys.argv; main(a[a.index("--home") + 1] if "--home" in a else None)
