"""term_of_art_scanner — Stage 1.5: catch term-of-art AMBIGUITY weaponized in filings/orders.

A word can be ordinary English, a statutory term of art, a common-law term of art, or a procedural
shorthand whose meaning depends on the rule invoked. Courts/counsel exploit the slippage — e.g.
"failure to state a claim" (Rule 12(b)(6) + Twombly/Iqbal PLAUSIBILITY) gets used to mean "meritless",
or "frivolous" (a defined §1915 term) gets used to mean "the judge disagrees". This scanner flags every
term-of-art occurrence in a text and supplies (a) its proper elements, (b) the common misuse, and
(c) the corrective authority — so misuse in an ORDER/opponent's brief becomes a challenge, and misuse
in OUR OWN draft gets fixed before filing.

Deterministic detection (no LLM). Corrective authority can be GROUNDED from the corpus with --ground
(semantic search_chunks). Non-destructive: scans a file or a case's order/OCR text; writes a report.

CLI:
  python -m pipeline.term_of_art_scanner --file path/to/order.txt
  python -m pipeline.term_of_art_scanner --slug fed_26-cv-00456      # scan the case's orders/OCR
  python -m pipeline.term_of_art_scanner --slug fed_26-cv-00456 --ground   # + corpus-ground correctives
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
AUDIT = ROOT / "output" / "audit"
CASES = ROOT / "obsidian_sync" / "cases"
OUT = ROOT / "output" / "term_of_art_scans"
NOTES = ROOT / "obsidian_sync" / "studies" / "term_of_art"

# Catalog: term -> {pattern, proper (elements/standard), misuse (how it's weaponized), corrective (cite +
# the seed to ground it)}. Reference data (extensible), not case data.
_TERMS = [
    {"term": "failure to state a claim", "pattern": r"fail(?:ure|s|ed)? to state a claim",
     "proper": "Rule 12(b)(6): do the alleged facts, taken as TRUE, plausibly satisfy each element? (Twombly/Iqbal PLAUSIBILITY, not probability, not merit).",
     "misuse": "Used to mean 'meritless' or 'I disagree with the theory' — and the qualifier 'upon which relief can be granted' is silently dropped.",
     "corrective": "Bell Atlantic v. Twombly 550 U.S. 544; Ashcroft v. Iqbal 556 U.S. 662; pro se: Haines v. Kerner; 3d Cir. notice-and-amend Alston v. Parker 363 F.3d 229",
     "seed": "Rule 12(b)(6) failure to state a claim plausibility Twombly Iqbal accept allegations as true notice and opportunity to amend pro se"},
    {"term": "frivolous", "pattern": r"\bfrivolous\b",
     "proper": "28 U.S.C. 1915(e)(2)(B): an action is frivolous only if it lacks an ARGUABLE BASIS in law OR fact (Neitzke v. Williams).",
     "misuse": "Used as a conclusory label for 'the court disagrees', without the no-arguable-basis analysis.",
     "corrective": "Neitzke v. Williams 490 U.S. 319; Denton v. Hernandez 504 U.S. 25",
     "seed": "frivolous 1915 arguable basis in law or fact Neitzke meritless distinction"},
    {"term": "holder in due course", "pattern": r"holder in due course|person entitled to enforce|\bholder\b",
     "proper": "UCC 3-301 distinguishes THREE: 'holder', 'holder in due course' (3-302), and 'person entitled to enforce' — conflated at peril; foreclosure requires the PETE.",
     "misuse": "'Holder' asserted as if it proved the right to enforce/foreclose without showing PETE status or HDC elements.",
     "corrective": "UCC 3-301; 3-302 (HDC elements); 3-309 (lost instrument); real party in interest Fed. R. Civ. P. 17(a)",
     "seed": "UCC 3-301 person entitled to enforce holder holder in due course 3-302 negotiable instrument foreclosure standing"},
    {"term": "standing", "pattern": r"\bstanding\b",
     "proper": "Three distinct doctrines: Article III standing (injury/causation/redressability), statutory standing, prudential standing — and 'real party in interest' (Rule 17(a)) is separate again.",
     "misuse": "One kind of 'standing' analysis substituted for another, or merits dressed as a standing defect.",
     "corrective": "Lujan v. Defenders of Wildlife 504 U.S. 555; Spokeo v. Robins 578 U.S. 330; Fed. R. Civ. P. 17(a)",
     "seed": "Article III standing injury in fact causation redressability statutory prudential real party in interest Rule 17"},
    {"term": "with prejudice / without leave to amend", "pattern": r"with prejudice|without leave to amend|denied leave to amend",
     "proper": "Dismissal WITH prejudice / without leave bars refiling; in the 3d Cir. a pro se plaintiff must get notice + an opportunity to amend before dismissal unless amendment is futile.",
     "misuse": "First-time dismissal made final without the required leave-to-amend opportunity.",
     "corrective": "Alston v. Parker 363 F.3d 229 (3d Cir.); Phillips v. County of Allegheny 515 F.3d 224; Grayson v. Mayview 293 F.3d 103",
     "seed": "dismissal with prejudice leave to amend futility pro se notice opportunity Alston Parker Third Circuit"},
    {"term": "abuse of discretion / de novo / clear error", "pattern": r"abuse of discretion|de novo|clear error|plain error",
     "proper": "Standards of review with distinct elements + who bears what — applying the wrong standard shifts the outcome and is itself reversible.",
     "misuse": "A summary-judgment / deferential standard imported into a motion-to-dismiss (different record, burden, review).",
     "corrective": "Pierce v. Underwood 487 U.S. 552; Anderson v. Bessemer City 470 U.S. 564; United States v. Olano 507 U.S. 725",
     "seed": "standard of review abuse of discretion de novo clear error plain error harmless burden"},
    {"term": "judicial notice", "pattern": r"judicial notice",
     "proper": "FRE 201: a court MUST take judicial notice if requested and supplied with the necessary info (201(c)(2)); adjudicative facts not subject to reasonable dispute.",
     "misuse": "Court refuses mandatory judicial notice, or notices a disputed fact as if indisputable.",
     "corrective": "Fed. R. Evid. 201(c)(2) (mandatory on request); 201(b) (indisputable facts only)",
     "seed": "Federal Rule of Evidence 201 judicial notice mandatory request adjudicative fact not subject to reasonable dispute"},
    {"term": "capacity — who the statute reaches (driver/motor vehicle/'person' as defined)",
     "pattern": r"\b(driver|operator|motor vehicle|commercial vehicle|transportation|for hire|in commerce)\b|the term \"?(?:person|individual)\"?",
     "proper": "A regulatory statute reaches a person ONLY in the capacity it regulates ('only to the extent that the person acts in such capacity', 12 U.S.C.). Terms like 'driver'/'motor vehicle'/'transportation' are frequently defined COMMERCIALLY (a motor carrier provides transportation FOR COMPENSATION; a license is authority to pursue a trade/profession/calling), whereas the public right to travel is absolute and paramount. Pull the statute's OWN definition and test the actor's actual capacity (commercial vs private).",
     "misuse": "A commercially-defined term (driver/motor vehicle/transportation) or a defined 'person'/'individual' applied to a PRIVATE, non-commercial act as if the ordinary meaning controlled — reaching someone the statute does not regulate; a commercial-offense charge asserted against a non-commercial act.",
     "corrective": "Am. Jur. 2d Automobiles s 175 (motor carrier = for compensation); Am. Jur. 2d Highways s 147 (right to travel absolute/paramount); Black's (license = trade/profession/calling); 1 U.S.C. 1 / the statute's own definition. Run pipeline.statute_capacity.analyze(terms, actor_capacity) for the verdict.",
     "seed": "motor carrier transportation for compensation for hire commercial driver private travel right to travel absolute paramount license privilege trade profession calling capacity applies only to the extent the person acts in such capacity"},
]


def scan_text(text: str) -> list[dict]:
    """Find every term-of-art occurrence; return flags with proper meaning + misuse + corrective."""
    flags, low = [], (text or "")
    for t in _TERMS:
        for m in re.finditer(t["pattern"], low, re.I):
            s = max(0, m.start() - 60); e = min(len(low), m.end() + 60)
            flags.append({"term": t["term"], "match": m.group(0),
                          "context": low[s:e].replace("\n", " ").strip(),
                          "proper": t["proper"], "misuse": t["misuse"],
                          "corrective": t["corrective"], "seed": t["seed"]})
    return flags


def _case_text(slug: str) -> str:
    """Gather the case's order / filing text from the audit JSON + any OCR sidecars (no PDF pulls)."""
    parts = []
    ap = AUDIT / f"data_{slug}.json"
    if not ap.exists():
        for c in AUDIT.glob(f"data_*{slug.split('_')[-1]}*.json"):
            ap = c; break
    if ap.exists():
        d = json.loads(ap.read_text(encoding="utf-8"))
        for o in (d.get("orders") or []):
            if isinstance(o, dict):
                parts.append(o.get("title", "") + " " + str(o.get("text", "")))
        for v in (d.get("procedural_violations") or []):
            parts.append(v.get("description", ""))
    for cdir in CASES.glob(f"*{slug.split('_')[-1]}*"):
        for ocr in (cdir / "intake").glob("*.ocr.txt") if (cdir / "intake").exists() else []:
            try:
                parts.append(ocr.read_text(encoding="utf-8", errors="ignore")[:20000])
            except Exception:
                pass
    return "\n".join(parts)


def _ground(seed: str):
    try:
        from pipeline.case_comprehension import _retrieve
        from beezle_mcp_server import _retr_ctx
        idx, ids, con, model = _retr_ctx()
        hits = _retrieve(idx, ids, con, model, seed, k=2)
        return [{"source": h.get("source", ""), "text": (h.get("text") or "")[:200]} for h in hits if isinstance(h, dict)]
    except Exception as e:
        return [{"source": "UNGROUNDED", "text": str(e)[:80]}]


def scan_case(slug: str, ground: bool = False) -> dict:
    flags = scan_text(_case_text(slug))
    # de-dup by (term, context)
    seen, uniq = set(), []
    for f in flags:
        k = (f["term"], f["context"][:50])
        if k not in seen:
            seen.add(k); uniq.append(f)
    if ground:
        for f in uniq:
            f["grounded"] = _ground(f["seed"])
    OUT.mkdir(parents=True, exist_ok=True); NOTES.mkdir(parents=True, exist_ok=True)
    (OUT / f"toa_scan_{slug}.json").write_text(json.dumps({"slug": slug, "flags": uniq}, indent=2), encoding="utf-8")
    md = [f"# Term-of-art scan — {slug}",
          "Each flagged term: its proper elements, the common misuse, and the corrective authority. "
          "Misuse in an order/opponent brief = a challenge; misuse in our draft = fix before filing.", ""]
    for f in uniq:
        md += [f"## \"{f['match']}\"  ({f['term']})",
               f"- context: …{f['context']}…",
               f"- **proper**: {f['proper']}",
               f"- **misuse**: {f['misuse']}",
               f"- **corrective**: {f['corrective']}"]
        if f.get("grounded"):
            md.append(f"- corpus: {f['grounded'][0]['source']}")
        md.append("")
    (NOTES / f"term_of_art_scan_{slug}.md").write_text("\n".join(md), encoding="utf-8")
    return {"slug": slug, "flags": uniq}


if __name__ == "__main__":
    ground = "--ground" in sys.argv
    if "--file" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--file") + 1])
        flags = scan_text(p.read_text(encoding="utf-8", errors="ignore"))
        print(f"{len(flags)} term-of-art flags in {p.name}")
        for f in flags[:20]:
            print(f"  - \"{f['match']}\" ({f['term']})")
    else:
        slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else "fed_26-cv-00456"
        r = scan_case(slug, ground=ground)
        print(f"term-of-art scan {slug}: {len(r['flags'])} flags")
        for f in r["flags"][:20]:
            print(f"  - \"{f['match']}\" ({f['term']})")
        print(f"  -> {OUT / ('toa_scan_'+slug+'.json')}")
