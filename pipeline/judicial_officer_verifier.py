"""judicial_officer_verifier — VERIFY (do not assume) the officer presiding in each case.

The system was trusting the intake file's "Judge: X" label. This reads the signer off the
case record, then CHECKS it against cases_index/judicial_officers.json — a registry built by
RESEARCH against authoritative sources (Federal Judicial Center, congress.gov nomination records,
state court rosters). An officer NOT in that registry is flagged UNVERIFIED (research required),
never assumed valid. Classifies Article III / magistrate / ALJ / elected-state and flags only a
REAL authority defect (unconfirmed, off the bench, magistrate final judgment without consent).

  python -m pipeline.judicial_officer_verifier            # all registered cases
  python -m pipeline.judicial_officer_verifier fed:2:26-cv-00456

R2-clean: all officer FACTS live in the JSON (sourced), not in this logic. Engine is generic.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
REG = ROOT / "cases_index" / "judicial_officers.json"
CASE_REG = ROOT / "cases_index" / "case_registry.json"
AUDIT = ROOT / "output" / "audit"
CASES = ROOT / "obsidian_sync" / "cases"

_TYPE_LABEL = {
    "ARTICLE_III_DISTRICT": "Article III U.S. District Judge",
    "ARTICLE_III_CIRCUIT": "Article III U.S. Circuit (appellate) Judge",
    "ARTICLE_III_SCOTUS": "Article III Supreme Court Justice",
    "MAGISTRATE": "U.S. Magistrate Judge (28 U.S.C. 631; not Article III)",
    "ALJ": "Administrative Law Judge (executive; not a court)",
    "STATE_ELECTED_CCP": "Elected State Trial Judge (Court of Common Pleas)",
    "STATE_APPELLATE": "State Appellate Judge",
}


def _registry() -> dict:
    try:
        return (json.loads(REG.read_text(encoding="utf-8")) or {}).get("officers", {})
    except Exception:
        return {}


def _name_tokens(name: str):
    """Normalize 'DOE, JANE' / 'John Q. Doe' -> (first, last) lowercased, no middle initial."""
    n = re.sub(r"\b(judge|justice|hon\.?|honorable|the)\b", " ", (name or ""), flags=re.I)
    n = re.sub(r"\([^)]*\)", " ", n)          # drop parentheticals like (GJP)
    n = re.sub(r"[^A-Za-z,\s.]", " ", n).strip()
    if "," in n:                               # LAST, FIRST
        last, _, first = n.partition(",")
    else:
        parts = [p for p in re.split(r"\s+", n) if p and not re.fullmatch(r"[A-Za-z]\.?", p)]
        if not parts:
            return None
        if len(parts) == 1:                    # bare surname (e.g. "Doe")
            return ("", re.sub(r"\.", "", parts[0]).lower())
        first, last = parts[0], parts[-1]
    f = re.sub(r"\.", "", first.strip().split()[0]).lower() if first.strip() else ""
    l = re.sub(r"\.", "", last.strip().split()[-1]).lower() if last.strip() else ""
    return (f, l) if l else None


_BAD_NAME = re.compile(r"[\[\]<>]|judge.?s? name|insert|xxx|\bn/?a\b|unknown|\btbd\b|signature|"
                       r"\border\b|\bcourt\b|\bname\b", re.I)


def _bad_name(nm: str) -> bool:
    """True if nm is a placeholder / non-name (template slug, bracketed, fewer than 2 name words).
    Counts 2+-letter tokens anywhere so a middle initial (John Q. Doe) does not trip it."""
    return bool(not nm or _BAD_NAME.search(nm) or len(re.findall(r"[A-Za-z]{2,}", nm)) < 2)


def _match(name: str, officers: dict):
    """Match a record name to a registry key by (first, last), tolerant of middle initials and bare
    surnames. A bare surname matches a verified officer by last name; genuinely-unknown names do not."""
    nt = _name_tokens(name)
    if not nt:
        return None, None
    for key, rec in officers.items():
        kt = _name_tokens(rec.get("name", key))
        if not kt or kt[1] != nt[1]:
            continue
        # last names match; first matches, or either side has no first (bare surname)
        if not nt[0] or not kt[0] or kt[0] == nt[0] or kt[0].startswith(nt[0]) or nt[0].startswith(kt[0]):
            return key, rec
    return None, None


def extract_signers(case_id: str) -> list:
    """Pull candidate judge/signer names from the case RECORD (registry + CASE_FACTS + audit)."""
    names, seen = [], set()

    def add(x):
        x = (x or "").strip()
        k = x.lower()
        if x and k not in seen and len(x) > 3:
            seen.add(k); names.append(x)

    try:
        reg = json.loads(CASE_REG.read_text(encoding="utf-8"))
        entry = reg.get(case_id) or next((v for k, v in reg.items()
                                          if isinstance(v, dict) and case_id in (k, v.get("canonical_id"))), {})
        if isinstance(entry, dict) and entry.get("judge"):
            add(entry["judge"])
    except Exception:
        pass
    digits = re.findall(r"\d{4,}", case_id)
    for cf in CASES.glob("*/CASE_FACTS*.md"):
        if any(d in cf.parent.name for d in digits):
            for m in re.finditer(r"\*\*?Judge\*?\*?:?\**\s*([A-Z][A-Za-z.\-]+(?:[,\s]+[A-Z][A-Za-z.\-]+){0,3})",
                                 cf.read_text(encoding="utf-8", errors="ignore")):
                add(m.group(1))
    return names


def _to_wsl(p: str) -> str:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p or "")
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/") if m else (p or "")


# signature-block markers; the TITLE is what proves Article III vs magistrate vs neither.
_TITLE_PATTERNS = [
    (r"United States District Judge", "ARTICLE_III_DISTRICT"),
    (r"U\.?\s?S\.?\s?D\.?\s?J\.?", "ARTICLE_III_DISTRICT"),
    (r"United States Circuit Judge", "ARTICLE_III_CIRCUIT"),
    (r"United States Magistrate Judge", "MAGISTRATE"),
    (r"U\.?\s?S\.?\s?M\.?\s?J\.?", "MAGISTRATE"),
    (r"Administrative Law Judge", "ALJ"),
    (r"Judge,?\s+Court of Common Pleas", "STATE_ELECTED_CCP"),
]


def extract_order_signers(case_id: str, max_docs: int = 24) -> list:
    """Pull the per-order authoring judge. PRIMARY source: order_indexes/orders_<slug>.json `filer`
    (already extracted by the order engine, with ECF). FALLBACK: parse the signature block of the
    order PDF (title text = District vs Magistrate vs ALJ). verify() then checks each against the
    researched registry — the order's filer is still a CLAIM to verify, never assumed."""
    out, seen = [], set()
    digits = re.findall(r"\d{4,}", case_id)

    # 1. PRIMARY — order index `filer`
    oidir = AUDIT / "order_indexes"
    of = next((p for p in oidir.glob("orders_*.json") if any(d in p.stem for d in digits)), None) \
        if oidir.exists() else None
    if of:
        try:
            data = json.loads(of.read_text(encoding="utf-8"))
            orders = data if isinstance(data, list) else (
                data.get("orders") or next((v for v in data.values() if isinstance(v, list)), []))
            for o in orders if isinstance(orders, list) else []:
                if not isinstance(o, dict):
                    continue
                filer = (o.get("filer") or "").strip()
                nm = re.sub(r",?\s*(J\.?|U\.?S\.?D\.?J\.?|U\.?S\.?M\.?J\.?)\s*$", "", filer).strip()
                doc = o.get("doc_id") or o.get("document") or ""
                # skip template placeholders ("[Judge's Name]", "insert", etc.) and non-names
                if not nm or doc in seen or _bad_name(nm):
                    continue
                seen.add(doc)
                out.append({"document": doc, "signer": nm, "ecf": o.get("ecf_number"),
                            "source": "order_index", "pdf_title_type": None})
        except Exception:
            pass

    # 2. FALLBACK — parse the order PDF signature block (for docs not covered by the index)
    try:
        from pipeline.doc_reader import extract_text
    except Exception:
        extract_text = None
    docs = []
    try:
        cm = json.loads((ROOT / "cases_index" / "case_map.json").read_text(encoding="utf-8"))
        ck = _resolve_in_map(case_id, cm)
        if ck:
            docs += (cm.get(ck, {}).get("orders") or [])
    except Exception:
        pass
    for p in AUDIT.glob("data_*.json"):
        if any(d in p.stem for d in digits):
            try:
                docs += [x.get("source") for x in (json.loads(p.read_text(encoding="utf-8")).get("dismissal") or [])
                         if isinstance(x, dict) and x.get("source")]
            except Exception:
                pass
            break
    bad = re.compile(r"order|court|so\b|it is|hereby|plaintiff|defendant|respectfull|attorney|"
                     r"pro se|wherefore|movant|petitioner|filer|filer", re.I)
    if extract_text:
        for raw in docs[:max_docs]:
            fp = Path(_to_wsl(raw))
            if not fp.exists() or fp.name in seen:
                continue
            seen.add(fp.name)
            try:
                tail = (extract_text(fp) or "")[-2600:]
            except Exception:
                continue
            by_the_court = "by the court" in tail.lower()
            for pat, typ in _TITLE_PATTERNS:
                m = re.search(pat, tail, re.I)
                if not m:
                    continue
                pre = tail[:m.start()]
                sm = re.search(r"/s/\s*([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){1,3})", pre)
                nm = None
                if sm and not bad.search(sm.group(1)):
                    nm = sm.group(1)
                elif by_the_court:
                    caps = re.findall(r"\b([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){1,3})\b", pre[-200:])
                    nm = next((c for c in reversed(caps) if not bad.search(c)), None)
                if not (sm and nm) and not (by_the_court and nm):
                    continue
                out.append({"document": fp.name, "signer": nm, "ecf": None,
                            "source": "signature_block", "pdf_title_type": typ,
                            "title_found": m.group(0)})
                break
    # dedupe by (signer-surname, document)
    uniq, keys = [], set()
    for r in out:
        st = (_name_tokens(r["signer"] or "") or ("", ""))[1]
        k = (st, r.get("document"))
        if k not in keys:
            keys.add(k); uniq.append(r)
    return uniq


def _resolve_in_map(case_id, cm):
    digits = re.findall(r"\d{4,}", case_id)
    for k, v in cm.items():
        if not isinstance(v, dict):
            continue
        if case_id in (k, v.get("case_id")) or any(d in (v.get("case_id", "") + k) for d in digits):
            return k
    return None


def verify(case_id: str, write: bool = True) -> dict:
    officers = _registry()
    signers = extract_signers(case_id)
    results = []
    seen_keys, seen_surnames, seen_unknown = set(), set(), set()
    for nm in signers:
        key, rec = _match(nm, officers)
        if rec:
            if key in seen_keys:               # same verified officer named twice -> dedupe
                continue
            seen_keys.add(key)
            kt = _name_tokens(rec.get("name", key))
            if kt:
                seen_surnames.add(kt[1])
        else:
            uk = (_name_tokens(nm) or ("", nm.lower()))[1]
            if uk in seen_surnames or uk in seen_unknown:  # surname of a verified officer / dup unknown
                continue
            seen_unknown.add(uk)
        if not rec:
            results.append({
                "named_in_record": nm,
                "verified": False,
                "classification": "UNVERIFIED",
                "defect": "NOT IN VERIFIED REGISTRY — research required against FJC / congress.gov / "
                          "state court roster before relying on this person's authority. Do NOT assume.",
                "sources": [],
            })
            continue
        defects = list(rec.get("defects") or [])
        if rec.get("status") and "active" not in str(rec.get("status")).lower():
            defects.append(f"STATUS: {rec['status']} — confirm the officer was seated when the order issued.")
        if rec.get("type") == "MAGISTRATE":
            defects.append("MAGISTRATE: may enter FINAL judgment only on the parties' consent (28 U.S.C. "
                           "636(c)). Confirm a signed consent (AO 85) is on the docket for any final order.")
        oath = rec.get("oath_453_filed") or rec.get("oath_filed")
        results.append({
            "named_in_record": nm,
            "officer": rec.get("name"),
            "verified": bool(rec.get("verified")),
            "classification": _TYPE_LABEL.get(rec.get("type"), rec.get("type")),
            "court": rec.get("court"),
            "selection": rec.get("selection"),
            "status": rec.get("status"),
            "authority": rec.get("authority"),
            "oath_status": oath,
            "appointment": {k: rec.get(k) for k in
                            ("appointed_by", "nominated", "confirmed", "commissioned", "nomination_record",
                             "elected", "assumed_office", "term_end") if rec.get(k)},
            "defects": defects,
            "sources": rec.get("sources") or [],
            "verified_on": rec.get("verified_on"),
        })

    # who actually filed/signed each order — verified against the researched registry, not assumed
    order_signers = [o for o in extract_order_signers(case_id) if not _bad_name(o.get("signer"))]
    magistrate_orders = []
    for osr in order_signers:
        key, rec = _match(osr.get("signer") or "", officers)
        pdf_t = osr.get("pdf_title_type")
        if rec:
            osr["verified"] = bool(rec.get("verified"))
            osr["registry_type"] = rec.get("type")
            osr["classification"] = _TYPE_LABEL.get(rec.get("type"), rec.get("type"))
            if pdf_t and pdf_t != rec.get("type"):
                osr["flag"] = (f"TITLE MISMATCH: the order's signature reads {pdf_t} but {rec.get('name')} "
                               f"is registered as {rec.get('type')} — investigate who actually signed.")
            elif rec.get("type") == "MAGISTRATE":
                osr["flag"] = ("MAGISTRATE-signed: a final/dispositive order needs the parties' consent "
                               "(28 U.S.C. 636(c), AO 85). Confirm a signed consent is on the docket.")
        else:
            osr["verified"] = False
            osr["classification"] = "UNVERIFIED"
            osr["flag"] = ("Order's filer is NOT in the verified registry — research this signer against "
                           "FJC / congress.gov / court roster before relying on the order. Do NOT assume.")
        if osr.get("registry_type") == "MAGISTRATE" or pdf_t == "MAGISTRATE":
            magistrate_orders.append(osr.get("document"))

    out = {"case_id": case_id, "signers_found": signers, "officers": results,
           "order_signers": order_signers,
           "magistrate_signed_orders": magistrate_orders,
           "all_verified": bool(results) and all(r["verified"] for r in results),
           "needs_research": [r["named_in_record"] for r in results if not r["verified"]]}

    if write:
        # 1. into the audit JSON (single source of truth)
        for p in AUDIT.glob("data_*.json"):
            if any(d in p.stem for d in re.findall(r"\d{4,}", case_id)):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    d["judicial_officers"] = out
                    p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                    out["_written_to"] = p.name
                except Exception:
                    pass
                break
        # 2. an Obsidian note (link layer)
        try:
            from pipeline.connected_notes import write_note
            lines = [f"# Judicial officer verification — {case_id} (researched, not assumed)\n"]
            for r in out["officers"]:
                lines.append(f"## {r.get('officer') or r['named_in_record']}  [{r['classification']}]")
                if r["verified"]:
                    lines.append(f"- Court: {r.get('court')}")
                    lines.append(f"- Selection: {r.get('selection')}")
                    if r.get("appointment"):
                        lines.append("- Appointment: " + "; ".join(f"{k}={v}" for k, v in r["appointment"].items()))
                    lines.append(f"- Status: {r.get('status')} | Authority: {r.get('authority')}")
                    lines.append(f"- Oath: {r.get('oath_status')}")
                for df in r.get("defects") or []:
                    lines.append(f"- ⚠ {df}")
                if r.get("sources"):
                    lines.append("- Sources: " + " ; ".join(r["sources"]))
                if not r["verified"]:
                    lines.append(f"- ⚠ {r.get('defect')}")
                lines.append("")
            if out.get("order_signers"):
                lines.append("## Who actually filed/signed each order (verified vs the researched registry)")
                for osr in out["order_signers"]:
                    tag = osr.get("classification") or osr.get("pdf_title_type") or "?"
                    ecf = f" (ECF {osr.get('ecf')})" if osr.get("ecf") else ""
                    v = "verified" if osr.get("verified") else "UNVERIFIED"
                    lines.append(f"- {osr.get('document')}{ecf}: **{osr.get('signer') or '?'}** "
                                 f"[{tag}] — {v} (src: {osr.get('source')})")
                    if osr.get("flag"):
                        lines.append(f"    - ⚠ {osr['flag']}")
                lines.append("")
            write_note("\n".join(lines), tags=["judicial-officer", "verification", "article iii",
                       "magistrate", "alj", "oath", case_id], source="judicial-officer-verifier")
        except Exception:
            pass
    return out


def _cases():
    try:
        return list(json.loads(CASE_REG.read_text(encoding="utf-8")).keys())
    except Exception:
        return []


if __name__ == "__main__":
    targets = sys.argv[1:] or _cases() or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                           "fed:2:26-cv-00789", "phila_ccp:260100000"]
    for cid in targets:
        r = verify(cid, write=True)
        print(f"\n=== {cid} ===")
        for o in r["officers"]:
            tag = "OK" if o["verified"] else "UNVERIFIED"
            print(f"  [{tag}] {o.get('officer') or o['named_in_record']} — {o['classification']}")
            for df in o.get("defects") or []:
                print(f"        defect: {df[:90]}")
            if not o["verified"]:
                print(f"        -> {o.get('defect','')[:90]}")
        for osr in r.get("order_signers") or []:
            v = "OK" if osr.get("verified") else "UNVERIFIED"
            cls = osr.get("classification") or osr.get("pdf_title_type") or "?"
            ecf = f" ECF{osr.get('ecf')}" if osr.get("ecf") else ""
            print(f"    order [{v}] {osr.get('signer') or '?'} [{cls}]{ecf} — {osr.get('document','')[:40]}")
            if osr.get("flag"):
                print(f"        ⚠ {osr['flag'][:86]}")
        if r.get("_written_to"):
            print(f"  written: {r['_written_to']}")
