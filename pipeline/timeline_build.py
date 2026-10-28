"""timeline_build — deterministic CASE_TIMELINE.json from a case's intake/ folder (no LLM).
Entry shape (matches the documented structure): sha1, filename, rel_path, mtime_iso, docket, date_filed, type, size_bytes, filer.
Existing entries are kept (matched by sha1); new intake files are appended; nothing is deleted. `docket` comes from the filename
or the OCR sidecar ("ECF 142", "Dkt. 179", "Document 12"); `date_filed` from a date in the filename, else the file's mtime.
The model may later enrich `type`/`filer`; this builder never overwrites a non-empty value."""
import hashlib, json, re, sys, time
from datetime import datetime
from pathlib import Path
from pipeline import paths as _P
DOCKET_RX = re.compile(r"(?i)\b(?:ecf|dkt\.?|docket|document|doc\.?)\s*(?:no\.?\s*)?(\d{1,4})\b")
DATE_RX = re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})|(\d{2})[-_.](\d{2})[-_.](\d{4})")
def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        while chunk := f.read(65536): h.update(chunk)
    return h.hexdigest()
def _date_from(name: str):
    m = DATE_RX.search(name)
    if not m: return None
    if m.group(1): return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return f"{m.group(6)}-{m.group(4)}-{m.group(5)}"
def build(case_dir: Path) -> dict:
    intake = case_dir / "intake"; tl_path = case_dir / "CASE_TIMELINE.json"
    d = {"updated": None, "entries": []}
    if tl_path.exists():
        try:
            old = json.loads(tl_path.read_text(encoding="utf-8"))
            d["entries"] = old.get("entries", old.get("timeline", [])) if isinstance(old, dict) else old
        except Exception: pass
    known = {e.get("sha1") for e in d["entries"] if isinstance(e, dict)}
    added = 0
    if intake.exists():
        for p in sorted(intake.iterdir()):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith((".doctext.txt", ".ocr.txt")): continue
            sha = _sha1(p)
            if sha in known: continue
            side = intake / "_ocr" / (p.name + ".txt"); text = side.read_text(encoding="utf-8", errors="ignore")[:4000] if side.exists() else ""
            m = DOCKET_RX.search(p.name) or DOCKET_RX.search(text)
            d["entries"].append({"sha1": sha, "filename": p.name, "rel_path": "intake\\" + p.name, "mtime_iso": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                                 "docket": int(m.group(1)) if m else None, "date_filed": _date_from(p.name) or datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d"),
                                 "type": "unknown", "size_bytes": p.stat().st_size, "filer": ""})
            known.add(sha); added += 1
    d["entries"].sort(key=lambda e: (str(e.get("date_filed") or e.get("date") or ""), str(e.get("filename"))))
    d["updated"] = datetime.now().isoformat(timespec="seconds"); d["added_last_run"] = added
    tl_path.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    return d
def main(slugs=None):
    for c in sorted(_P.CASES.iterdir()) if _P.CASES.exists() else []:
        if c.is_dir() and not c.name.startswith(("_", "-")) and (not slugs or c.name in slugs):
            d = build(c); print(f"  timeline {c.name}: {len(d['entries'])} entries (+{d['added_last_run']})")
if __name__ == "__main__": main(sys.argv[1:] or None)
