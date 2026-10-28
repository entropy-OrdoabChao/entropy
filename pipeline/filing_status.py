#!/usr/bin/env python3
"""
pipeline/filing_status.py

Filing-status gate for m0. Lets the drafter KNOW what has already been filed so it advances the
case (e.g. to the appeal) instead of re-drafting a motion that is already on the docket.

Signal hierarchy (user's rule, 2026-07-06):
  1. PRIMARY  — a CONSOLIDATED PDF exists for the stub. Consolidation is the last step before the
     user mails/files, so a consolidated *_FILING.pdf in the mail-ready trees == it went out.
  2. REDUNDANCY — an email notification / docket event corroborates it landed: an ECF NEF captured
     in the submission ledger or the case's docket_events.md, or (state court, no ECF) a CASE_TIMELINE
     order/"filed" event naming the motion.

Design rules:
  - R2 (no hardcoded case data): case identity comes from case_slug + cases_index/case_registry.json,
    never inline judge/party literals.
  - Never raises. Conservative: returns filed=False / 'unknown' on any error, so the gate can only
    SKIP a draft on positive evidence, never silently drop one on a miss.
"""
from __future__ import annotations
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]

# Trees where consolidated, mail-ready PDFs are assembled. Raw intake + dedup-quarantine are excluded
# at scan time (they are NOT our outgoing filings).
_CONSOLIDATED_ROOT_GLOBS = [
    "obsidian_sync/_FILINGS_READY_*",
    "output/filings",
    "output/accountability_drafts/_consolidated",
    "obsidian_sync/cases/*/drafts/_consolidated_mailing",   # per-court mailing packages (letter stream)
]
_EXCLUDE_PATH_MARKERS = ("_dedup_quarantine", "/intake/", "\\intake\\", "_archive", ".bak")


def _norm_stub(name: str) -> str:
    """Normalize a stub / draft / consolidated-PDF filename to a comparable key.
    'chain_99_m0_selected__00_OMNIBUS_motion_lippi_FILING.pdf' -> 'omnibus_motion_lippi'."""
    s = Path(str(name)).stem
    s = re.sub(r"_(FILING|DRAFT)$", "", s, flags=re.I)
    s = re.split(r"__", s)[-1]                       # drop chain/packet prefix before '__'
    s = re.sub(r"^(chain_\d+_m0_selected_?)", "", s, flags=re.I)
    s = re.sub(r"^\d+[_-]+", "", s)                  # leading '00_'
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _registry_tokens(case_slug: str) -> set:
    """Case-identity tokens for path matching: slug parts + docket digits + registry caption/party
    words (e.g. 'lippi'). Data-driven from case_registry.json — no inline case data."""
    toks = {t for t in re.split(r"[_:\-]", case_slug.lower()) if len(t) >= 3}
    m = re.search(r"(\d{6,})", case_slug)
    if m:
        toks.add(m.group(1))
    try:
        reg = json.loads((REPO / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
        cases = reg.get("cases", reg) if isinstance(reg, dict) else reg
        rows = cases.values() if isinstance(cases, dict) else cases
        for c in rows:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("canonical_id") or c.get("case_id") or "")
            slugized = re.sub(r"[^a-z0-9]+", "_", cid.lower())
            if case_slug.lower() in slugized or (m and m.group(1) in cid):
                for field in ("caption", "short_name", "parties"):
                    val = c.get(field)
                    if isinstance(val, str):
                        for w in re.findall(r"[A-Za-z]{4,}", val):
                            wl = w.lower()
                            if wl not in ("state", "farm", "company", "court", "county", "plaintiff",
                                          "defendant", "versus", "civil", "action"):
                                toks.add(wl)
                break
    except Exception:
        pass
    return toks


def consolidated_filings(case_slug: str) -> Dict[str, List[str]]:
    """{norm_stub_key: [consolidated_pdf_path, ...]} for PDFs that belong to this case.
    A key present here == that filing was consolidated for mailing (PRIMARY 'filed' evidence)."""
    toks = _registry_tokens(case_slug)
    out: Dict[str, List[str]] = {}
    for g in _CONSOLIDATED_ROOT_GLOBS:
        for base in REPO.glob(g):
            if not base.exists():
                continue
            for pdf in base.rglob("*.pdf"):
                p = str(pdf).lower()
                if any(mk in p for mk in _EXCLUDE_PATH_MARKERS):
                    continue
                if not any(t in p for t in toks):
                    continue
                out.setdefault(_norm_stub(pdf.name), []).append(str(pdf))
            # LETTER STREAM: a per-court package bundles many filings under ONE PDF, so also read its
            # manifest.json and register EACH component filing (Notice of Appeal, IFP, brief, ...) as mailed,
            # so m0 skips re-drafting anything that already went out in a consolidated package.
            for manifest in base.rglob("manifest.json"):
                mp = str(manifest).lower()
                if any(mk in mp for mk in _EXCLUDE_PATH_MARKERS) or not any(t in mp for t in toks):
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    continue
                bundle = str((manifest.parent / f"consolidated_{data.get('court_slug','')}.pdf"))
                for label, comp_path in (data.get("filings") or []):
                    out.setdefault(_norm_stub(Path(comp_path).name), []).append(bundle)
    return out


def _timeline_events(case_slug: str) -> List[dict]:
    p = REPO / "obsidian_sync" / "cases" / case_slug / "CASE_TIMELINE.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return d.get("timeline") or d.get("entries") or []


def _notification_corroborates(case_slug: str, norm_key: str) -> Optional[str]:
    """REDUNDANCY: an email/docket notification or a timeline order/'filed' event naming this motion."""
    words = [w for w in norm_key.split("_") if len(w) >= 4]
    # a) docket_events.md (federal ECF NEFs captured by docket_email_fetch)
    de = REPO / "obsidian_sync" / "cases" / case_slug / "docket_events.md"
    if de.exists():
        try:
            body = de.read_text(encoding="utf-8").lower()
            if words and all(w in body for w in words[:2]):
                return "docket_events.md notification"
        except Exception:
            pass
    # b) CASE_TIMELINE order / filed events (state court has no ECF NEF)
    for e in _timeline_events(case_slug):
        ev = str(e.get("event") or "").lower()
        if not ev:
            continue
        if re.search(r"\bfiled\b|denied|granted|order", ev) and words and any(w in ev for w in words):
            return f"timeline: {e.get('date')} {e.get('event')}"
    # c) LETTER STREAM: a LetterStream/OnlineCertifiedMail confirmation whose free-text label matches this
    #    case (case-identity tokens + the filing's own words) — direct proof the piece was physically mailed.
    try:
        from pipeline.letterstream_mail_fetch import is_mailed
        case_desc = " ".join(_registry_tokens(case_slug)) + " " + norm_key.replace("_", " ")
        hit = is_mailed(case_desc)
        if hit:
            return f"LetterStream mailed: '{hit['label']}' ({hit.get('date','')[:16]})"
    except Exception:
        pass
    return None


_APPEAL_VEHICLE_RE = re.compile(
    r"appeal|appellate|petition_for_allowance|allocatur|mandamus|certiorari|cert_petition|frap_\d|"
    r"petition_for_review|superior_court|third_cir|3rd_cir|scotus",
    re.I)


def is_appeal_vehicle(stub_name: str) -> bool:
    """True if the stub is an appellate vehicle (notice of appeal, appellate brief, mandamus, cert…)."""
    return bool(_APPEAL_VEHICLE_RE.search(_norm_stub(stub_name)))


def case_phase(case_slug: str, today: str = "") -> str:
    """'appeal' if the case has crossed into the appellate track — a future NOTICE OF APPEAL / appeal
    deadline, or a notice-of-appeal docket event — else 'trial'. Data-driven from the timeline +
    docket_events; no hardcoded case data."""
    for d in active_deadlines(case_slug, today):
        if re.search(r"appeal", d["event"], re.I):
            return "appeal"
    for e in _timeline_events(case_slug):
        if re.search(r"notice of appeal|record on appeal|docketing.*appeal",
                     str(e.get("event") or e.get("type") or ""), re.I):
            return "appeal"
    de = REPO / "obsidian_sync" / "cases" / case_slug / "docket_events.md"
    if de.exists():
        try:
            if re.search(r"appeal", de.read_text(encoding="utf-8"), re.I):
                return "appeal"
        except Exception:
            pass
    return "trial"


def stub_status(case_slug: str, stub_name: str, today: str = "") -> Dict[str, Any]:
    """Filing status for one stub in the case's CURRENT phase. Returns:
       {status, key, pdf, primary, corroboration, phase, appeal_vehicle}
    status is one of:
       'active'      — the item to draft now (matches the current phase; e.g. an appeal vehicle while
                       the case is on appeal). Draft it.
       'filed'       — a consolidated PDF exists and it belongs to a PRIOR phase (e.g. a trial motion
                       once the case is on appeal). Do not re-draft as new.
       'consolidated'— consolidated but phase-ambiguous (consolidated in current phase; likely already
                       mailed — warn, don't hard-skip).
       'not_started' — no consolidated PDF yet. Draft it.
    """
    key = _norm_stub(stub_name)
    pdfs = consolidated_filings(case_slug).get(key, [])
    corrob = _notification_corroborates(case_slug, key)
    phase = case_phase(case_slug, today)
    appeal_veh = is_appeal_vehicle(stub_name)
    if phase == "appeal" and appeal_veh:
        status = "active"                      # the appeal is what we file now — always draftable
    elif phase == "appeal" and not appeal_veh and pdfs:
        status = "filed"                       # trial motion; case now on appeal; was consolidated -> prior phase
    elif pdfs:
        status = "consolidated"                # consolidated in-phase: probably mailed already (warn)
    else:
        status = "not_started"
    return {
        "status": status,
        "key": key,
        "pdf": pdfs,
        "primary": pdfs[0] if pdfs else "",
        "corroboration": corrob,
        "phase": phase,
        "appeal_vehicle": appeal_veh,
        "filed": status in ("filed", "consolidated"),
    }


def _docket_deadlines(case_slug: str) -> List[dict]:
    """V1/L12: deadlines carried by docket_events.md's notice-only NEFs (appeal/briefing triggers). The
    grounded source for the FRAP-31 clock — the CASE_TIMELINE `type` values are document kinds, not
    deadlines. Computes the appellant opening-brief due date deterministically (FRAP 31(a)(1): 40 days
    after the record is docketed) from the docketed date on the trigger line."""
    import datetime
    dv = REPO / "obsidian_sync" / "cases" / case_slug / "docket_events.md"
    out: List[dict] = []
    if not dv.exists():
        return out
    for ln in dv.read_text(encoding="utf-8", errors="ignore").splitlines():
        up = ln.upper()
        if "DEADLINE-TRIGGER" not in up and "FRAP 31" not in up:
            continue
        md = re.search(r"(20\d{2}-\d{2}-\d{2})", ln)
        if not md:
            continue
        base = datetime.date.fromisoformat(md.group(1))
        usca = re.search(r"USCA\s+(\d{2}-\d{3,5})", ln)
        num = f" (USCA {usca.group(1)})" if usca else ""
        if re.search(r"FRAP\s*31|briefing clock|opening-brief", ln, re.I):
            due = base + datetime.timedelta(days=40)   # FRAP 31(a)(1): 40 days after the record is filed
            out.append({"date": due.isoformat(),
                        "event": f"FRAP 31 appellant opening brief due{num} — 40 days after record docketed {base.isoformat()}"})
    return out


def active_deadlines(case_slug: str, today: str = "") -> List[dict]:
    """Future DEADLINE events, soonest first. today = 'YYYY-MM-DD' (caller passes it; this module never
    calls Date.now). Reads BOTH the CASE_TIMELINE entries (robust to field-name convention: type/date_filed
    or event/date) AND the docket_events.md briefing-clock triggers (where the FRAP-31 deadline lives)."""
    out = []
    for e in _timeline_events(case_slug):
        ev = str(e.get("event") or e.get("type") or "")            # V1: entries use `type`, not `event`
        dt = str(e.get("date") or e.get("date_filed") or "")       # V1: entries use `date_filed`, not `date`
        if re.search(r"deadline|due\b", ev, re.I) and dt:
            if not today or dt >= today:
                out.append({"date": dt, "event": ev})
    for d in _docket_deadlines(case_slug):                          # V1/L12: docket-derived FRAP clock
        if not today or d["date"] >= today:
            out.append(d)
    # de-dup and sort soonest-first
    seen, uniq = set(), []
    for d in sorted(out, key=lambda x: x["date"]):
        k = (d["date"], d["event"])
        if k not in seen:
            seen.add(k); uniq.append(d)
    return uniq


def posture_block(case_slug: str, today: str = "", stub_names: Optional[List[str]] = None) -> str:
    """Markdown grounding block for the drafter prompt: what is already FILED (a prior phase — do not
    re-plead as new), what is ACTIVE now, and the next hard DEADLINE — so the LLM writes in the correct
    posture (e.g. appellant on appeal)."""
    cons = consolidated_filings(case_slug)
    dls = active_deadlines(case_slug, today)
    phase = case_phase(case_slug, today)
    if not cons and not dls:
        return ""
    filed, active = [], []
    for k in sorted(cons):
        (active if (phase == "appeal" and is_appeal_vehicle(k)) else filed).append(k.replace("_", " "))
    parts = [f"## FILING POSTURE — current phase: {phase.upper()}"]
    if dls:
        parts.append("**Next deadline(s):**")
        for d in dls[:3]:
            parts.append(f"- {d['date']} — {d['event']}")
    if active:
        parts.append("\n**ACTIVE now (draft/advance these):** " + "; ".join(sorted(set(active))))
    if filed:
        parts.append("\n**Already on the docket (prior phase — do NOT re-plead as new relief):**")
        parts.append("  " + "; ".join(sorted(set(filed))))
    parts.append("\nWrite in the CURRENT posture. If the case is on appeal, argue to the appellate "
                 "court as appellant assigning error to the order below; do not re-file trial motions "
                 "already decided.")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    import sys
    slug = sys.argv[1] if len(sys.argv) > 1 else "phila_ccp_260100000"
    today = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"=== consolidated (FILED) for {slug} ===")
    for k, v in sorted(consolidated_filings(slug).items()):
        print(f"  [{k}]  <- {len(v)} pdf(s)")
    print(f"\n=== active deadlines (>= {today or 'any'}) ===")
    for d in active_deadlines(slug, today):
        print(f"  {d['date']} — {d['event']}")
    print("\n=== posture block ===")
    print(posture_block(slug, today))
