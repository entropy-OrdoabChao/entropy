"""Attribute each detected violation to its AUTHOR — plaintiff (you) / judge /
opposing counsel — so the drafter attacks ONLY opposing-counsel + judge
violations and NEVER the litigant's own filings.

Joins:
  - document_role_classifier.audit_case()  -> per-source-doc role
  - CASE_GROUND_TRUTH.yaml judge + opposing_counsel blocks -> the specific actor

Adds to each violation: document_role, attributed_actor, attack (bool).
Violations from your own filings get attack=False and are never targeted.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys
from pathlib import Path
from collections import Counter
import yaml

try:
    from pipeline.fiduciary_engine import is_fiduciary as _is_fiduciary, match_breaches as _fid_breaches
except Exception:  # engine optional; attribution still works without it
    def _is_fiduciary(_role): return False
    def _fid_breaches(*_a, **_k): return []

ROOT = _eroot()
if str(ROOT) not in sys.path:           # importable when run directly, too
    sys.path.insert(0, str(ROOT))
_CASES = ROOT / "obsidian_sync" / "cases"

# Who the drafter is allowed to attack. Conservative: only judge + opposing.
ROLE_ATTACK = {
    "pro_se_plaintiff_filing": False, "plaintiff": False,   # NEVER attack your own
    "judicial_output": True,  "judge": True,                # judicial canon target
    "opposing_party_filing": True, "opposing_counsel": True,  # opposing target
    "exhibit_or_evidence": False, "correspondence": False,
    "transcript": False,
    # unknown: the classifier reliably flags YOUR filings as pro_se, so an
    # unknown is almost certainly opposing/court -> recover as a target, but
    # tag UNVERIFIED so the drafter confirms the source before attacking.
    "unknown": True,
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _basename_stem(sd: str) -> str:
    """Basename without extension, robust to BOTH \\ and / separators
    (scorecard source_file is a full Windows path; WSL Path won't split \\)."""
    base = re.split(r"[\\/]", sd or "")[-1]
    return re.sub(r"\.(pdf|docx?|md|txt|php|json)$", "", base, flags=re.I)


def _resolve_path(sd: str) -> Path:
    """Resolve a source string to a real path. Converts Windows paths
    (C:\\Users\\...) to WSL (/mnt/c/Users/...) so files outside the case
    dir (e.g. the user's Documents/pdf) can be classified directly."""
    s = (sd or "").strip()
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return Path(f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/"))
    return Path(s.replace("\\", "/"))


def classify_by_content(txt: str) -> str:
    """Determine the author/role of a filing from its (OCR'd) text — caption +
    signature block. Order: opposing (attorney-for-defendant signature) wins,
    then judicial (by-the-court order), then plaintiff (pro se)."""
    if not txt:
        return "unknown"
    t = (txt[:9000] + "\n" + txt[-3500:])
    tl = t.lower()
    # OPPOSING — authoritative signature: "Attorney(s)/Counsel for Defendant/Garnishee"
    if re.search(r"(attorneys?|counsel)\s+for\s+(defendant|garnishee|respondent)", tl) \
       or re.search(r"on behalf of\s+(defendant|garnishee|respondent)", tl):
        return "opposing_party_filing"
    # JUDICIAL — order/opinion language + judicial signature, no party-attorney block
    if (re.search(r"\bby the court\b", tl) or re.search(r"\band now,?\s+this\b", tl)
            or (re.search(r"it is\s+(hereby\s+)?ordered", tl)
                and re.search(r",\s*j\.\s*$", t.strip(), re.M | re.I))):
        return "judicial_output"
    # PLAINTIFF (your own) — protect: pro se / sui juris / Filer as plaintiff
    if re.search(r"\bpro se\b", tl) or re.search(r"\bsui juris\b", tl) \
       or re.search(r"(plaintiff|filer)[,\s].{0,40}pro se", tl):
        return "pro_se_plaintiff_filing"
    return "unknown"


_OCR_CACHE_PATH = ROOT / "output" / "audit" / "_attrib_role_ocr_cache.json"


def _load_ocr_cache() -> dict:
    try:
        return json.loads(_OCR_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ocr_cache(cache: dict) -> None:
    try:
        _OCR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OCR_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:
        pass


def _ocr_role(path: Path) -> str:
    """OCR an image-only PDF and classify its author by content. Cached."""
    try:
        from pipeline.ocr_engine import ocr_pdf, is_ocr_available
        if not is_ocr_available():
            return "unknown"
        txt = ocr_pdf(path) or ""
        return classify_by_content(txt)
    except Exception:
        return "unknown"


def _gt(case_id: str) -> dict:
    for d in sorted(_CASES.glob("*/")):
        g = d / "meta" / "CASE_GROUND_TRUTH.yaml"
        if not g.exists():
            continue
        try:
            data = yaml.safe_load(g.open(encoding="utf-8")) or {}
        except Exception:
            continue
        if data.get("case_id") == case_id:
            return data
    return {}


def _opposing_actor(source_doc: str, gt: dict) -> str:
    sd = _norm(source_doc)
    for oc in (gt.get("opposing_counsel") or []):
        src = _norm((str(oc.get("source") or "")).split(":", 1)[-1])
        if src and (src in sd or sd in src):
            return (f"{oc.get('attorney', '?')}, {oc.get('firm', '?')} "
                    f"(for {oc.get('represents', '?')})")
    return "Opposing counsel"


# In-process memo so multiple motion drafts in one run don't re-audit the same case.
_ROLE_MAP_MEM: dict = {}


def _build_role_map(case_id: str) -> dict:
    """Per-source-doc role map. CACHED: the full document re-classification (audit_case reads text
    from every case file — hundreds of PDFs) only runs when the case's audit JSON is refreshed
    (i.e. a new document landed). Otherwise the map is served from disk instantly. Invalidation is
    O(1): compare the cache file's mtime against output/audit/data_<slug>.json (rewritten by
    audit_writer whenever case state changes). This preserves the exact prior behavior on the first
    build; only repeated builds of an unchanged case are short-circuited."""
    from pipeline.document_role_classifier import audit_case
    if case_id in _ROLE_MAP_MEM:
        return _ROLE_MAP_MEM[case_id]
    slug = case_id.replace(":", "_")
    cache_p = ROOT / "output" / "audit" / f"role_map_{slug}.json"
    audit_p = ROOT / "output" / "audit" / f"data_{slug}.json"
    try:
        if cache_p.exists():
            fresh = (not audit_p.exists()) or (cache_p.stat().st_mtime >= audit_p.stat().st_mtime)
            if fresh:
                m = (json.loads(cache_p.read_text(encoding="utf-8")) or {}).get("map") or {}
                _ROLE_MAP_MEM[case_id] = m
                return m
    except Exception:
        pass  # any cache trouble -> fall through and rebuild (never wrong, just slower)
    res = audit_case(case_id) or {}
    m = {}
    for doc in (res.get("documents") or []):
        m[_norm(Path(doc["file"]).stem)] = doc.get("role", "unknown")
    try:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(json.dumps({"map": m}), encoding="utf-8")
    except Exception:
        pass
    _ROLE_MAP_MEM[case_id] = m
    return m


def attribute_violations(case_id: str, write_back: bool = False) -> tuple[dict, dict]:
    gt = _gt(case_id)
    judge = (gt.get("judge") or {}).get("name") or "the presiding judge"
    rmap = _build_role_map(case_id)
    slug = case_id.replace(":", "_")
    audit_p = ROOT / "output" / "audit" / f"data_{slug}.json"
    audit = json.loads(audit_p.read_text(encoding="utf-8")) if audit_p.exists() else {}

    _role_cache: dict = {}
    _classify = None
    _ocr_cache = _load_ocr_cache()
    _ocr_dirty = [False]

    def _has_counsel_sig(path: Path) -> bool:
        """Check if a PDF contains a counsel signature block anywhere in its text.
        This prevents combined PDFs (judicial order + opposing brief) from being
        mis-attributed to the judge. See document_role_classifier counsel_override.
        """
        if not path.exists():
            return False
        try:
            if path.suffix.lower() == ".pdf":
                try:
                    from pypdf import PdfReader
                    r = PdfReader(str(path))
                    full_text = ""
                    for page in r.pages:
                        full_text += (page.extract_text() or "") + "\n"
                        if len(full_text) > 100000:
                            break
                    tl = full_text.lower()
                    return bool(re.search(
                        r"(counsel|attorney(?:s?)?)\s+for\s+(defendant|garnishee|respondent)", tl)
                        or re.search(r"on\s+behalf\s+of\s+(defendant|garnishee|respondent)", tl)
                        or re.search(r"represents?\s+(defendant|garnishee|respondent)", tl)
                        or re.search(r"counsel\s+for\s+[a-z][a-z\s]+(?:company|corporation|llc|insurance|trust|bank|group)", tl)
                        or re.search(r"attorneys?\s+for\s+[a-z][a-z\s]+(?:company|corporation|llc|insurance|trust|bank|group)", tl))
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def role_for(sd: str) -> str:
        nonlocal _classify
        key = sd or ""
        if key in _role_cache:
            return _role_cache[key]
        n = _norm(_basename_stem(sd))
        role = None
        if n:
            role = rmap.get(n) or next(
                (r for k, r in rmap.items() if k and (k in n or n in k)), None)
        if not role or role == "unknown":
            try:
                p = _resolve_path(sd)
                if p.exists():
                    if _classify is None:
                        from pipeline.document_role_classifier import classify as _classify
                    role = _classify(p, case_id=case_id).role
                    if role in (None, "unknown"):
                        # image-only PDF -> OCR + content/signature author classify (cached)
                        if n in _ocr_cache:
                            role = _ocr_cache[n]
                        else:
                            role = _ocr_role(p)
                            _ocr_cache[n] = role
                            _ocr_dirty[0] = True
            except Exception:
                pass
        role = role or "unknown"
        # SAFEGUARD: even if the classifier says judicial_output, check if the
        # file contains a counsel signature block anywhere. If so, override to
        # opposing_party_filing. This catches combined PDFs where a judge's order
        # is bundled with opposing counsel's brief (e.g., h73ELbL6J.pdf).
        if role == "judicial_output" and n:
            try:
                p = _resolve_path(sd)
                if not p.exists():
                    # source_doc may be a bare filename; search case intake
                    for candidate in [
                        Path(f"<home>/cases/phila_ccp_260100000/intake/{n}.pdf"),
                        Path(f"<home>/cases/phila_ccp_260100000/intake/{n}"),
                        Path(f"/mnt/c/Users/malic/Documents/pdf/misc/{n}.pdf"),
                        Path(f"/mnt/c/Users/malic/Documents/pdf/misc/{n}"),
                    ]:
                        if candidate.exists():
                            p = candidate
                            break
                if p.exists() and _has_counsel_sig(p):
                    role = "opposing_party_filing"
            except Exception:
                pass
        _role_cache[key] = role
        return role

    summ = Counter()

    def tag(v: dict, src_key: str):
        sd = v.get(src_key) or ""
        role = role_for(sd)
        attack = ROLE_ATTACK.get(role, False)
        if role in ("judicial_output", "judge"):
            actor = f"Judge {judge}"
        elif role in ("opposing_party_filing", "opposing_counsel"):
            actor = _opposing_actor(sd, gt)
        elif role in ("pro_se_plaintiff_filing", "plaintiff"):
            actor = "Plaintiff (own filing — NOT an attack target)"
        elif role == "unknown":
            actor = "Opposing counsel / court (UNVERIFIED — confirm source before filing)"
        else:
            actor = "(context / evidence)"
        v["document_role"] = role
        v["attributed_actor"] = actor
        v["attack"] = attack
        # FIDUCIARY_MARK: judge / court / opposing counsel are fiduciaries to the
        # tribunal and parties. Mark the actor + attach the DB-backed duty its
        # conduct breaches (Caperton/Liteky/Murchison/Rule 3.3/etc.). Never marks
        # the litigant's own filings (attack=False short-circuits).
        if attack and _is_fiduciary(role):
            v["fiduciary"] = True
            sig = (v.get("violation_type") or v.get("category")
                   or v.get("type") or v.get("name") or "")
            breaches = _fid_breaches(role, sig)
            if breaches:
                v["fiduciary_breach"] = breaches
        summ[("ATTACK" if attack else "EXCLUDE") + " :: " + role] += 1

    for v in (audit.get("procedural_violations") or []):
        tag(v, "source_doc")
    for v in ((audit.get("scorecard") or {}).get("violations") or []):
        tag(v, "source_file")

    if _ocr_dirty[0]:
        _save_ocr_cache(_ocr_cache)

    if write_back and audit_p.exists():
        audit_p.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    return audit, dict(summ)


if __name__ == "__main__":
    import sys
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                 "fed:2:26-cv-00789", "phila_ccp:260100000"]):
        _, s = attribute_violations(cid)
        print(f"\n{cid}")
        for k in sorted(s):
            print(f"   {k:42} {s[k]}")
