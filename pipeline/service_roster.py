"""service_roster — who gets served, at what address, and WHERE each row came from.

Fed. R. App. P. 25(d)(1)(B): proof of service must state "the name of the person served" and "the address" used.
A hand-typed list is a forgery risk and a staleness risk; this module derives the roster from the record:

  names     = bundle serve_list_<appeal>.txt  (m0)  ∪  CA3 Clerk-order "cc:" lists (CASE_TIMELINE.json, forum CA3)
  addresses = efiling/letterstream_final/<appeal>_*/manifest.json (what was actually mailed)
              else the district docket's attorney blocks (intake/*docket*.doctext.txt / docket_report*.html)
              else "address NOT IN RECORD"  (kept in the roster and reported — never silently dropped)

Every row carries its `source`. The filer, the clerk and chambers are never recipients (filing office / self).
Hook: m1_finisher.service_recipients(stub_path) -> roster_rows(...).
"""
from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path

_PHONE_RE = re.compile(r"(\(\d{3}\)\s*\d{3}-\d{4}|\b\d{3}-\d{3}-\d{4}\b)")
_TERM_RE = re.compile(r"^\s*TERMINATED:\s*\d{2}/\d{2}/\d{4}\s*")
_JUNK_RE = re.compile(r"^(--- Page|https?://|\d{1,2}/\d{1,2}/\d{2,4}, \d{1,2}:\d{2} (AM|PM)|Query Reports|Date Filed)")
_MARKERS = {"LEAD ATTORNEY", "ATTORNEY TO BE NOTICED", "PRO SE", "Defendant", "Plaintiff", "V.", "Movant",
            "Interested Party", "Appellant", "Appellee"}
_PERSON_RE = re.compile(r"^[A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-\.]*){1,3}$")
_SELF_RE = re.compile(r"(?i)\b(office of the clerk|clerk of court|clerk\b|honorable|chambers|judicial conduct|circuit executive)")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


def _key(name: str) -> tuple[str, str]:
    """(first initial, last name) — the docket prints 'JOSHUA DAVID HILL', the clerk 'Joshua D. Hill'."""
    toks = [t for t in _norm(name.split(",")[0]).split() if t not in ("esq", "jr", "sr", "ii", "iii")]
    if not toks:
        return ("", "")
    return (toks[0][:1], toks[-1])


def _initials(name: str) -> str:
    toks = [t for t in _norm(name.split(",")[0]).split() if t not in ("esq", "jr", "sr", "ii", "iii")]
    return "".join(t[0] for t in toks).upper()


# ---------------------------------------------------------------- district docket attorney blocks
def _docket_text_sources(case_dir: Path) -> list[tuple[Path, list[str]]]:
    out = []
    intake = case_dir / "intake"
    if not intake.is_dir():
        return out
    for p in sorted(intake.glob("*docket*.doctext.txt")) + sorted(intake.glob("*docket*.txt")):
        if p in [o[0] for o in out]:
            continue
        out.append((p, p.read_text(encoding="utf-8", errors="replace").splitlines()))
    for p in sorted(intake.glob("docket_report*.html")):
        t = _html.unescape(re.sub(r"<[^>]+>", "\n", p.read_text(encoding="utf-8", errors="replace")))
        out.append((p, t.splitlines()))
    return out


def district_counsel(case_dir: Path) -> dict[tuple[str, str], dict]:
    """{(first initial, last): {name, firm, address, source}} from every docket text in intake/."""
    found: dict[tuple[str, str], dict] = {}
    for path, raw in _docket_text_sources(case_dir):
        lines = []
        for l in raw:
            l = l.strip()
            if not l or _JUNK_RE.match(l):
                continue
            lines.append(l)
        i = 0
        while i < len(lines):
            l = lines[i]
            name = None
            m = re.search(r"\brepresented by\s+(.+)$", l)
            if m:
                name = m.group(1).strip()
            elif i > 0 and (lines[i - 1] in _MARKERS or _TERM_RE.match(lines[i - 1])) and _PERSON_RE.match(l) \
                    and l not in _MARKERS and not _PHONE_RE.search(l):
                name = l
            if not name or name.upper() == name and len(name.split()) < 2:
                i += 1
                continue
            # 'FILER NAME represented by FILER NAME' = the filer (PRO SE label is the docket's, not ours)
            block, j = [], i + 1
            while j < len(lines):
                x = _TERM_RE.sub("", lines[j]).strip()
                if not x or x in _MARKERS or x.startswith("TERMINATED") or x.startswith("(See above"):
                    j += 1
                    if x in _MARKERS or x.startswith("(See above"):
                        break
                    continue
                if _PHONE_RE.search(x) or x.lower().startswith(("email:", "fax:")):
                    break
                if re.search(r"\brepresented by\b", x) or _PERSON_RE.match(x) and len(block) >= 2 and re.search(r"\d{5}", block[-1]):
                    break
                block.append(x)
                j += 1
            k = _key(name)
            if block and k not in found:
                firm = block[0]
                addr = ", ".join(block[1:]) if len(block) > 1 else ""
                if re.search(r"\d{5}(-\d{4})?$", firm) and len(block) == 1:
                    firm, addr = "", block[0]
                found[k] = {"name": name.title() if name.isupper() else name, "firm": firm, "address": addr,
                            "source": f"district docket attorney block: {path.name}"}
            i = max(j, i + 1)
    return found


# ---------------------------------------------------------------- CA3 clerk-order cc lists
def ca3_cc_entries(case_dir: Path) -> list[dict]:
    """[{entry, appeal, date}] from CASE_TIMELINE events of forum CA3 that carry 'cc:' (initials or names)."""
    tl = case_dir / "CASE_TIMELINE.json"
    if not tl.exists():
        return []
    try:
        t = json.loads(tl.read_text(encoding="utf-8"))
    except Exception:
        return []
    events = t if isinstance(t, list) else (t.get("events") or [])
    out = []
    for e in events:
        if not isinstance(e, dict) or str(e.get("forum", "")).upper() != "CA3":
            continue
        txt = str(e.get("event", ""))
        m = re.search(r"\bcc:\s*(.+?)(?:\.\s*\[|\.\s*$|$)", txt)
        if not m:
            continue
        ap = re.search(r"No\.\s*(\d{2}-\d{4,5})", txt)
        for ent in re.split(r"\s*;\s*", m.group(1)):
            ent = ent.strip().rstrip(".")
            if ent:
                out.append({"entry": ent, "appeal": ap.group(1) if ap else "", "date": e.get("date", "")})
    return out


# ---------------------------------------------------------------- letterstream manifests
def letterstream_addresses(final_dir: Path, appno: str = "") -> dict[tuple[str, str], dict]:
    """{(fi, last): {display, address, source}} — manifests for THIS appeal first, then any with a full address."""
    out: dict[tuple[str, str], dict] = {}
    if not final_dir or not Path(final_dir).is_dir():
        return out
    tag = appno.split("-")[-1] if appno else ""
    mans = sorted(Path(final_dir).glob("*/manifest.json"), key=lambda m: (0 if tag and m.parent.name.startswith(tag + "_") else 1, m.parent.name))
    for m in mans:
        try:
            d = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        a = d.get("address") or {}
        parts = [a.get("line1"), a.get("line2"), a.get("city"), " ".join(y for y in (a.get("state"), a.get("zip")) if y)]
        addr = ", ".join(x for x in parts if x)
        if not a.get("line1") or not a.get("zip"):
            continue
        k = _key(d.get("recipient", ""))
        if k not in out:
            out[k] = {"display": d.get("recipient", ""), "address": addr,
                      "source": f"efiling/letterstream_final/{m.parent.name}/manifest.json"}
    return out


# ---------------------------------------------------------------- roster
def roster_rows(stub_path: Path, case_dir: Path, final_dir: Path, appno: str = "", self_name: str = "") -> dict:
    """-> {rows: [str], detail: [dict], unresolved: [dict], sources: [str]} for the certificate of service."""
    stub_path = Path(stub_path).resolve()
    dirs = [stub_path.parent] + list(stub_path.parents)[:4]
    cands = []
    if appno:
        for d in dirs:
            cands += sorted(d.glob(f"serve_list_{appno}.txt")) + sorted(d.glob(f"serve_list_{appno.split('-')[-1]}.txt"))
    for d in dirs:
        cands += sorted(d.glob("serve_list*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
    names: list[tuple[str, str]] = []   # (name, source)
    if cands:
        for l in cands[0].read_text(encoding="utf-8").splitlines():
            if l.strip():
                names.append((l.strip(), f"serve list {cands[0].name}"))
    counsel = district_counsel(case_dir)
    ls = letterstream_addresses(final_dir, appno)
    known = {_key(n): n for n, _ in names}
    known.update({k: v["name"] for k, v in counsel.items()})
    known.update({k: v["display"].split(",")[0] for k, v in ls.items()})
    cc = ca3_cc_entries(case_dir)
    # full names on any clerk order are counsel of record too (26-0002 spells out what 26-0000 abbreviates)
    for e in cc:
        if not re.fullmatch(r"[A-Z]{2,4}", e["entry"]):
            known.setdefault(_key(e["entry"]), e["entry"])
    by_init = {}
    for k, n in known.items():
        by_init.setdefault(_initials(n), n)
        by_init.setdefault((k[0] + k[1][:1]).upper(), n)      # first+last only, when the clerk drops the middle
    unresolved = []
    for e in cc:
        ent, src = e["entry"], f"CA3 No. {e['appeal']} Clerk's order {e['date']} cc list"
        if re.fullmatch(r"[A-Z]{2,4}", ent):
            full = by_init.get(ent) or (by_init.get(ent[0] + ent[-1]) if len(ent) == 3 else None)
            if not full:
                unresolved.append({"entry": ent, "source": src, "why": "initials match no counsel of record"})
                continue
            ent = full
        if _key(ent) not in {_key(n) for n, _ in names}:
            names.append((ent, src))
    self_key = _key(self_name) if self_name else ("", "")
    rows, detail = [], []
    seen = set()
    for n, src in names:
        k = _key(n)
        if k in seen or k == self_key or _SELF_RE.search(n) or _initials(n) == "MBT":
            continue
        seen.add(k)
        # the district docket is the record: its address wins over a LetterStream manifest (one appeal's manifest carried
        # Cornish at 19401 while the docket and the other appeal say 19404 — two certificates must never disagree)
        if k in counsel and counsel[k]["address"]:
            c = counsel[k]
            disp = f"{c['name']}, Esq." + (f", {c['firm']}" if c["firm"] else "")
            addr, asrc = c["address"], c["source"]
            if k in ls and _norm(ls[k]["address"]) != _norm(addr):
                print(f"  [roster] {c['name']}: manifest address differs from docket — docket used ({ls[k]['source']} vs {asrc})")
        elif k in ls:
            disp, addr, asrc = ls[k]["display"], ls[k]["address"], ls[k]["source"]
        else:
            disp, addr, asrc = (n if "," in n else f"{n}, Esq."), "address NOT IN RECORD", "NOT IN RECORD"
        rows.append(f"{disp} — {addr}")
        detail.append({"name": n, "display": disp, "address": addr, "name_source": src, "address_source": asrc})
    for u in unresolved:   # a cc on a clerk's order whose initials match no counsel of record is not a service recipient:
        print(f"  [roster] NOT SERVED: {u['entry']} ({u['source']}) — {u['why']}; resolve on the CA3 docket")   # reported, never printed on the COS
    return {"rows": rows, "detail": detail, "unresolved": unresolved,
            "sources": sorted({d["name_source"] for d in detail} | {d["address_source"] for d in detail})}
