# -*- coding: utf-8 -*-
"""
persona_status — WHICH KIND OF PERSON does the record treat the litigant as, and does that classification
move the case onto in rem / disability ground where the in-personam protections fall away.
(BACKLOG S-capacity-status + addendum, 2026-10-20.)

Every defined word is a term of art for the part that invokes it ("You means the debtor." 34 C.F.R. 34.3;
"You means a Farm Credit bank ..." 12 C.F.R. pt. 615). So the litigant's legal persona is not presumed: it
is READ from what the court and the opponent actually wrote, then tested against the controlling
definitions and the in rem / in personam limits.

Pipeline position: called by pipeline.jurisdiction_mode.analyze (m0 audit) -> key `persona_status`, and its
switch proofs are appended to `improper_switches` (the list m0 drafting already carries).

Division of labor (quality doctrine):
  * LLM (local-gpu legal lane, reasoning ON) = comprehension: who issued each record document, and what persona /
    mode / relief it assigns to the litigant, with a verbatim quote.
  * Deterministic = GUARDRAIL only: bounded pieces, a quote must be a verbatim substring of the record or the
    finding is dropped, the litigant's own filings never count, results cached per document hash.
Run alone:  /usr/bin/python3 -m pipeline.persona_status <slug> [--no-llm]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import hashlib, json, os, re, sys, urllib.request
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
AUDIT = ROOT / "output" / "audit"
CACHE = AUDIT / "persona_status"
CASES_OBS = ROOT / "obsidian_sync" / "cases"
CASES_RAW = ROOT / "cases"

PERSONAS = ("NATURAL_PERSON_SUI_JURIS", "ARTIFICIAL_PERSON", "ESTATE_OR_RES", "INFANT_WARD_DISABILITY",
            "DEFINED_TERM_DEBTOR", "UNSTATED")
MODES = ("IN_PERSONAM", "IN_REM", "QUASI_IN_REM", "UNSTATED")

# Verbatim corpus text (verified 2026-10-20 against chunk_text.db; chunk ids pinned).
DEFINITIONS = [
    ("person", "A man considered according to the rank he holds in society, with all the rights to which the "
               "place he holds entitles him, and the duties which it imposes.",
     "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_3770"),
    ("individual", "As a noun, this term denotes a single person as distinguished from a group or class, and "
                   "also, very commonly, a private or natural person as distinguished from a partnership, "
                   "corporation, or association; but it is said that this restrictive signification is not "
                   "necessarily inherent in the word, and that it may, in proper cases, include artificial "
                   "persons.", "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_2611"),
    ("corporation", "An artificial person or legal entity created by or under the authority of the laws of a "
                    "state or nation", "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_1157"),
    ("status", "The status of a person is his legal position or condition.",
     "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_4645"),
    ("estate / status", "The condition of persons. It also means estate, because it signifies the condition or "
                        "circumstances in which the owner stands with regard to his property.",
     "Bouvier's Law Dictionary (1856)", "bouvier_ESTATE_0_16e7e112"),
    ("estate (whose property is administered)", "the word \u201cestate\u201d may have several meanings, although it does "
     "not import a legal entity. It is generally used as meaning the property belonging to a decedent, a ward, a "
     "mentally incompetent person, or a bankrupt, which property is being administered in the courts.",
     "28 Am. Jur. 2d Estates \u00a7 1", "amjur2d_28_estates#e0b5b6ac5b49f8e0"),
    ("infant", "A person within age, not of age, or not of full age; a person under the age of twenty-one "
               "years; a minor.", "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_2624"),
    ("disability", "... under age, insane persons, and felons convict are said to be under disability.",
     "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_1581"),
    ("sui juris", "Of his own right; possessing full social and civil rights; not under any legal disability, "
                  "or the power of another, or guardianship.", "Black's Law Dictionary", "defs_index:sui juris"),
    ("non sui juris", "Not his own master. The opposite of sui juris.",
     "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_3499"),
    ("debtor", "One who owes a debt; he who may be compelled to pay a claim or demand.",
     "Black's Law Dictionary (2d ed.)", "blacks_law_2nd_chunk_1425"),
    ("you (defined term)", "You means the debtor.", "34 C.F.R. 34.3 (Authority: 31 U.S.C. 3720D)",
     "ECFR-title34#1ce74182c01c52773c16dc4a929902e0"),
]
RULES = {
    "in_rem_limit": ("If jurisdiction is based on the court's power over property within its territory, the "
                     "action is called 'in rem' or 'quasi in rem.' The effect of a judgment in such a case is "
                     "limited to the property that supports jurisdiction and does not impose a personal "
                     "liability on the property owner, since he is not before the court.",
                     "Shaffer v. Heitner, 433 U.S. 186, 199 (1977)", "SCOTUS_vol_USREPORTS-433_PDFA_chunk_1460"),
    "no_person_void": ("[without service or appearance the personal judgment is] 'coram non judice, and void'",
                       "Pennoyer v. Neff, 95 U.S. 714", "cap_us_95_714_pennoyer_v_neff#1ed12065f116089b"),
    "means_closed": ("It is axiomatic that the statutory definition of the term excludes unstated meanings of "
                     "that term", "Meese v. Keene, 481 U.S. 465, 484-85 (1987)",
                     "rules_of_statutory_construction_canons_chunk_5"),
    "exceptions_pa": ("Exceptions expressed in a statute shall be construed to exclude all others.",
                      "1 Pa.C.S. § 1924", "pa_consolidated_statutes#156915fc44d8d9e06254e5b3384df627"),
}
_WAIVER_NOTE = ("Personal jurisdiction is waivable by a general appearance or consent; before calling a personal "
                "order VOID, check the record for an appearance/merits filing/consent clause. Without one the "
                "personal portion is void; with one it is error (voidable) on a timed vehicle.")

PIECE = 6000          # chars per LLM piece (bounded — m0 works in pieces)
MAX_PIECES = 5        # per document
MAX_DOCS = 40
_V = "p2"              # prompt/version salt — a prompt change invalidates the per-doc cache


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _ask(prompt: str, timeout: int = 900) -> str:
    """local-gpu legal lane (same contract as accountability_map._ask_legal), reasoning ON: classifying the persona
    a document assigns is comprehension/derivation, not extraction."""
    url = os.environ.get("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
    model = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b")
    body = json.dumps({"model": model, "max_tokens": 6000, "temperature": 0.1,
                       "chat_template_kwargs": {"enable_thinking": True},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    raw = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    msg = raw["choices"][0].get("message", {})
    return (msg.get("content") or "").strip()


def _json_array(txt: str):
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _intake_file(slug: str, name: str):
    """Resolve a ledger source name to its text file under either case root (raw intake or vault intake)."""
    for d in (CASES_RAW / slug / "intake", CASES_OBS / slug / "intake"):
        if not d.exists():
            continue
        for p in (d / name, d / "court_orders" / name):
            if p.exists():
                return p
        stem = name[:60]
        hits = sorted(x for x in d.rglob(stem + "*") if x.suffix == ".txt")
        if hits:
            return hits[0]
    return None


def record_docs(slug: str, data: dict | None = None) -> list[dict]:
    """Documents the court/opponent wrote, from every record index the case has (data-driven, any case)."""
    docs, seen = [], set()
    led = CASES_OBS / slug / "_STATEMENT_LEDGER.json"
    if led.exists():
        try:
            st = json.loads(led.read_text(encoding="utf-8")).get("statements") or []
        except Exception:
            st = []
        for s in st:
            if s.get("author") not in ("court", "opponent"):
                continue
            src = s.get("source") or ""
            if src in seen:
                continue
            seen.add(src)
            p = _intake_file(slug, src)
            if p:
                docs.append({"doc": src, "text": p.read_text(encoding="utf-8", errors="ignore")})
    # every court order the vault holds, whether or not the ledger attributed it (the LLM still decides issuer)
    od = CASES_OBS / slug / "intake" / "court_orders"
    if od.exists():
        for p in sorted(od.glob("*.txt")):
            if p.name not in seen:
                seen.add(p.name)
                docs.append({"doc": p.name, "text": p.read_text(encoding="utf-8", errors="ignore")})
    data = data or {}
    for f in (data.get("opposing_facts") or []):
        if isinstance(f, dict) and f.get("text"):
            key = "OPPOSING_FACT:%s" % (f.get("source_doc") or "")[:40]
            docs.append({"doc": key, "text": f["text"]})
    odir = AUDIT / "order_indexes" / ("orders_%s.json" % slug)
    if odir.exists():
        try:
            o = json.loads(odir.read_text(encoding="utf-8"))
            for e in ((o.get("orders") if isinstance(o, dict) else o) or [])[:200]:
                if isinstance(e, dict):
                    docs.append({"doc": "ORDER:%s" % (e.get("doc_id") or "")[:40], "text": json.dumps(e)})
        except Exception:
            pass
    return docs[:MAX_DOCS]


_PROMPT = """You read ONE piece of a court record. Party context (from the case file):
{parties}

Definitions that control this analysis (verbatim):
{defs}

Task: decide (1) who ISSUED this document: COURT (judge/clerk/prothonotary order or notice), OPPOSING (the other
side or its counsel), SELF (the self-represented litigant's own filing), or OTHER; and (2) every place where the
document treats the SELF-REPRESENTED LITIGANT as a particular kind of person or proceeds against a THING instead
of the person. Persona values: {personas}. Mode values: {modes}.
Use comprehension, not keywords: a garnishment/attachment/writ against an account or wages proceeds against a
THING (ESTATE_OR_RES, IN_REM or QUASI_IN_REM); a money judgment, contempt, fees or a command addressed to the
litigant is relief against the PERSON; guardian/next friend/competency language is INFANT_WARD_DISABILITY;
treating the litigant as an entity/corporation/trust is ARTIFICIAL_PERSON; a statute-defined label ("debtor",
"you", "consumer") applied to the litigant is DEFINED_TERM_DEBTOR.

Return ONLY a JSON array (empty [] if nothing applies). Each item:
{{"issuer":"COURT|OPPOSING|SELF|OTHER","persona":"...","mode":"...","relief_against":"PERSON|PROPERTY|NONE",
 "quote":"<copied EXACTLY from the text below, 20-300 chars>","basis":"<one sentence: why this quote assigns that persona/mode>"}}

TEXT:
<<<
{text}
>>>"""


def _classify_doc(doc: dict, parties: str) -> dict:
    text = doc["text"]
    h = hashlib.sha1((_V + text).encode("utf-8", "ignore")).hexdigest()[:16]
    defs = "\n".join("- %s: \"%s\" (%s)" % (t, d, c) for t, d, c, _ in DEFINITIONS)
    findings, dropped, errors = [], 0, 0
    for i in range(0, min(len(text), PIECE * MAX_PIECES), PIECE):
        piece = text[i:i + PIECE]
        if len(piece.strip()) < 80:
            continue
        try:
            out = _ask(_PROMPT.format(parties=parties, defs=defs, personas=", ".join(PERSONAS),
                                      modes=", ".join(MODES), text=piece))
        except Exception:
            errors += 1
            continue
        for it in _json_array(out):
            if not isinstance(it, dict):
                continue
            q = _norm(it.get("quote"))
            # GUARDRAIL: verbatim or dropped; enums enforced
            if len(q) < 12 or q not in _norm(piece) or it.get("persona") not in PERSONAS:
                dropped += 1
                continue
            it["quote"] = q
            it["mode"] = it.get("mode") if it.get("mode") in MODES else "UNSTATED"
            it["doc"] = doc["doc"]
            findings.append(it)
    return {"hash": h, "findings": findings, "dropped_unverified": dropped, "llm_errors": errors}


def _proofs(findings: list[dict]) -> list[str]:
    """Deterministic rule step over the LLM's verified findings: law -> record -> violation -> lever."""
    proofs, by_doc = [], {}
    for f in findings:
        if f.get("issuer") in ("COURT", "OPPOSING"):
            by_doc.setdefault(f["doc"], []).append(f)
    for doc, fs in by_doc.items():
        res = [f for f in fs if f["mode"] in ("IN_REM", "QUASI_IN_REM") or f["persona"] == "ESTATE_OR_RES"]
        pers = [f for f in fs if f.get("relief_against") == "PERSON"]
        if res and pers:
            r = RULES["in_rem_limit"]
            proofs.append(
                "PROOF [RES-to-PERSON switch] by %s\n  LAW: \"%s\" %s [corpus:%s]\n  RECORD (%s): proceeds "
                "against the thing: \"%s\" — then reaches the person: \"%s\"\n  THEREFORE: relief against the "
                "person exceeds jurisdiction resting on the property.\n  LEVER: %s"
                % (res[0]["issuer"], r[0], r[1], r[2], doc, res[0]["quote"][:200], pers[0]["quote"][:200],
                   _WAIVER_NOTE))
        for f in fs:
            if f["persona"] == "INFANT_WARD_DISABILITY":
                proofs.append(
                    "PROOF [STATUS switch: disability] by %s\n  LAW: sui juris = \"not under any legal disability, "
                    "or the power of another, or guardianship\" (Black's); a disability status needs a predicate "
                    "finding on notice and hearing.\n  RECORD (%s): \"%s\"\n  THEREFORE: status assigned without a "
                    "shown predicate is a due-process defect.\n  LEVER: demand the predicate finding; object and "
                    "appeal on the timed vehicle." % (f["issuer"], doc, f["quote"][:240]))
            elif f["persona"] == "ARTIFICIAL_PERSON":
                proofs.append(
                    "PROOF [STATUS switch: artificial person] by %s\n  LAW: corporation = \"An artificial person "
                    "or legal entity created by or under the authority of the laws of a state\" (Black's 2d); "
                    "\"individual\" ordinarily = natural person, artificial only \"in proper cases\" (Black's 2d)."
                    "\n  RECORD (%s): \"%s\"\n  THEREFORE: the record must show the statute/context that reaches a "
                    "natural person as an entity.\n  LEVER: demand the controlling definition section; \"means\" "
                    "excludes unstated meanings (%s)." % (f["issuer"], doc, f["quote"][:240], RULES["means_closed"][1]))
            elif f["persona"] == "DEFINED_TERM_DEBTOR":
                proofs.append(
                    "PROOF [DEFINED-TERM persona] by %s\n  LAW: a defined word carries only the meaning of the part "
                    "that defines it (e.g. \"You means the debtor.\" 34 C.F.R. 34.3); %s.\n  RECORD (%s): \"%s\"\n"
                    "  THEREFORE: the label binds only if the defining part reaches this litigant.\n  LEVER: demand "
                    "the defining section and the facts placing the litigant inside it." %
                    (f["issuer"], RULES["means_closed"][1], doc, f["quote"][:240]))
    return proofs


def analyze(slug: str, data: dict | None = None, run_llm: bool | None = None) -> dict:
    if run_llm is None:
        run_llm = os.environ.get("BEEZLE_PERSONA_LLM", "1") == "1"
    CACHE.mkdir(parents=True, exist_ok=True)
    cpath = CACHE / ("%s.json" % slug)
    cache = {}
    if cpath.exists():
        try:
            cache = json.loads(cpath.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    facts = CASES_OBS / slug / "CASE_FACTS.md"
    parties = _norm(facts.read_text(encoding="utf-8", errors="ignore")[:1500]) if facts.exists() else slug
    docs = record_docs(slug, data)
    per_doc, pending = [], 0
    for d in docs:
        h = hashlib.sha1((_V + d["text"]).encode("utf-8", "ignore")).hexdigest()[:16]
        hit = cache.get(d["doc"])
        if hit and hit.get("hash") == h:
            per_doc.append(hit)
            continue
        if not run_llm:
            pending += 1
            continue
        r = _classify_doc(d, parties)
        r["doc"] = d["doc"]
        per_doc.append(r)
        if r.get("llm_errors"):                 # never cache a failed read (silent-empty class): retry next run
            continue
        cache[d["doc"]] = r
        cpath.write_text(json.dumps(cache, indent=1), encoding="utf-8")   # resumable per document
    findings = [f for r in per_doc for f in r.get("findings", [])]
    issued = [f for f in findings if f.get("issuer") in ("COURT", "OPPOSING")]
    errs = sum(r.get("llm_errors", 0) for r in per_doc)
    return {
        "status": "INCOMPLETE — LLM lane failed on %d piece(s); findings are NOT a clean result" % errs if errs
                  else "COMPLETE",
        "record_docs": len(docs), "classified_docs": len(per_doc), "pending_docs": pending,
        "dropped_unverified_quotes": sum(r.get("dropped_unverified", 0) for r in per_doc),
        "llm_errors": sum(r.get("llm_errors", 0) for r in per_doc),
        "persona_findings": issued,
        "persona_tally": {p: sum(1 for f in issued if f["persona"] == p) for p in PERSONAS},
        "switch_proofs": _proofs(findings),
        "definitions": [{"term": t, "text": d, "cite": c, "corpus": k} for t, d, c, k in DEFINITIONS],
        "rules": {k: {"text": v[0], "cite": v[1], "corpus": v[2]} for k, v in RULES.items()},
        "waiver_check": _WAIVER_NOTE,
    }


if __name__ == "__main__":
    s = next((a for a in sys.argv[1:] if not a.startswith("-")), "phila_ccp_260100000")
    out = analyze(s, run_llm="--no-llm" not in sys.argv)
    print(json.dumps({k: v for k, v in out.items() if k not in ("definitions", "rules")}, indent=1)[:12000])
