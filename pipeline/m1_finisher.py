#!/usr/bin/env python3
"""m1_finisher.py — m1 lane FINISHING: run an m1-assembled body through the
SAME deterministic document furniture m0 uses. NOT a reimplementation — this
imports m0's own organs so the two lanes render identically by construction.

M0 DOCUMENT ANATOMY (measured from FILING_INSTRUCTIONS_FILING.docx +
motion_drafter_v2 lines 2560-2620, 2026-08-26):
  1. CAPTION: court_full centered bold; two-column party block w/ ')' rail;
     Case No. + Judge on right; from generation.md_to_docx COURT_RULES.
  2. TITLE: centered bold (renderer re-emits from '# ' line).
  3. SCHEDULE A — DEFINITIONS: definitions_tagger.inject_into_draft.
  4. INTRODUCTION / STATEMENT OF FACTS: numbered ¶s '1.' '2.' (SOF numbering
     is in the md body itself), justified.
  5. ARGUMENT: Roman-numeral bold left headings 'I. ...' 'II. ...';
     body justified w/ superscript terms-of-art.
  6. PRAYER FOR RELIEF: 'WHEREFORE' + numbered requests.
  7. DEFECTS IN THE RECORD table (#/Defect/Rule/Count/Record Basis/
     Consequence) — from the audit stream (scorecard data).
  8. Signature block: 'Respectfully submitted,' + /s/ + rule line + name
     'Pro Se' + PREFILLED ADDRESS (123 Main St, Suite 100,
     Anytown, ST 00000) + 'Dated:' — via blocks/renderer, address from
     efiling/mail_config.json 'from' (single source, never hardcoded here).
  9. CERTIFICATE OF SERVICE (centered bold heading) + second sig block.
 10. [PROPOSED] ORDER ('AND NOW, this _____ day...' + 'BY THE COURT:').
 11. DEFINITIONS & AUTHORITIES footnote block: numbered entries
     'term [Dict.]/[Statute]/[Rule] — definition', bound to unicode
     superscripts in the body: definitions_tagger.inject_quantum(sup='unicode').
     QUANTUM GATE: argument filings only (motions/briefs/oppositions/replies);
     administrative docs (orders, COS, praecipes...) get neither Schedule A
     nor footnotes (motion_drafter_v2 _QUANTUM_SKIP).

Pipeline: m1 body md -> definitions inject (3,11) -> markdown skeleton in m0
order (1,2,4-10 via renderer) -> generation.md_to_docx.render_md_to_docx
(caption/spacing/fonts per COURT_RULES) -> m1_filing_gate check.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, sys, datetime
from pathlib import Path
from pipeline.m1_filing_gate import strip_provenance_tags   # CLASS GUARD A: one regex, gate + finisher + drafter

# the repo root is the first ancestor holding efiling/ — parent.parent is the SANDBOX when run under an overlay, which
# silently emptied the signature address (mail_config.json not found) on every sandboxed run
ROOT = next((p for p in Path(__file__).resolve().parents if (p / "efiling").is_dir()), Path(__file__).resolve().parent.parent)


def _case_root(case_id) -> Path:
    """Case directory for either spelling m1 passes: the obsidian slug (when run from a
    stub path) or the registry case_id "phila_ccp:260100001" (when run with --case).
    Three finisher organs built ROOT/obsidian_sync/cases/<case_id> directly, so with a
    case_id the stub, relief and CASE_FACTS were silently "not found" -- and the *210
    anatomy was never emitted (2026-09-27, the CCP opposition shipped without its five
    mandated sections). Resolves via cases_index/case_map.json (slug -> {case_id})."""
    import json as _json
    cid = str(case_id or "").strip()
    direct = ROOT / "obsidian_sync" / "cases" / cid
    if not cid or direct.is_dir():
        return direct
    num = cid.split(":", 1)[-1].strip()
    try:
        cm = _json.loads((ROOT / "cases_index" / "case_map.json").read_text(encoding="utf-8"))
    except Exception:
        cm = {}
    for slug, v in (cm.items() if isinstance(cm, dict) else []):
        if str((v or {}).get("case_id", "")).strip() == num and (ROOT / "obsidian_sync" / "cases" / slug).is_dir():
            return ROOT / "obsidian_sync" / "cases" / slug
    return direct

AFFIDAVIT_FILING_TYPES = {'affidavit', 'declaration', 'verification', 'sworn_statement'}
ADMIN_TYPES = {
    'civil_cover_sheet', 'cover_sheet', 'proposed_order', 'order',
    'certificate_of_service', 'certificate_of_compliance', 'proof_of_service',
    'praecipe', 'entry_of_appearance', 'notice_of_appeal', 'exhibit_cover',
    'exhibit_index', 'summons', 'ifp_application',
    # Pa.R.A.P. 123 applications / status inquiries state grounds and relief; they are
    # not "briefs or memoranda of law" and take no Schedule A / standard-of-review anatomy.
    'application_for_relief', 'application', 'status_inquiry', 'letter_to_clerk',
}

def _sig_address():
    """Prefilled signature address — SINGLE SOURCE: efiling/mail_config.json.

    The key is "return_address"; reading a non-existent "from" key yielded an
    empty dict, so every signature block rendered with NO address at all — the
    filer's address is required on a PA CCP filing (Pa.R.C.P. 1025) and its
    absence is a clerk-rejection defect. Accept either key so a future config
    rename cannot silently re-open the same hole, and drop empty lines.
    """
    try:
        mc = json.loads((ROOT / "efiling" / "mail_config.json").read_text())
        f = mc.get("return_address") or mc.get("from") or {}
        lines = [f.get("line1", ""), f.get("line2", "")]
        city = f"{f.get('city','')}, {f.get('state','')} {f.get('zip','')}".strip(", ")
        out = [l.strip() for l in lines if l and l.strip()]
        if city.strip(", "):
            out.append(city)
        # Phila.Civ.R. *210 requires the filer's ELECTRONIC MAIL ADDRESS on a brief
        # ("the name, address, and electronic mail address of the attorney or the party
        # if not represented by an attorney"), so the signature block carries it too.
        _em = (f.get("email") or "").strip()
        if _em:
            out.append(_em)
        _nm = (f.get("name") or "").strip()
        if not _nm or not out:
            raise KeyError("return_address.name/address missing")
        return _nm, out
    except Exception as _e:
        # NEVER a hardcoded filer: a missing registry is a visible defect, not a silent default
        # (the gate catches the placeholder; a wrong name on a filed paper it would not).
        print(f"  [signature] WARNING: efiling/mail_config.json return_address unreadable ({_e}) — placeholder emitted")
        return "[FILER NAME NOT IN REGISTRY]", ["[FILER ADDRESS NOT IN REGISTRY]"]

def signature_block(dated=None, court=None):
    """CAPACITY DESIGNATION (user-locked 2026-08-26, see
    obsidian_sync/analysis/CAPACITY_SUI_JURIS_ANALYSIS.md):
    sui juris (capacity) + explicit 1-308 reservation + personal appearance
    under 28 USC 1654. NEVER 'pro se' self-label, NEVER bar number/Esq./
    bar-membership certificate (filer is not an attorney)."""
    name, addr = _sig_address()
    d = dated or datetime.date.today().strftime("%B %d, %Y")
    _court = (court or "").upper()
    from pipeline import forum_profile as _fp
    _sov = _fp.sovereign_of(_court)
    _is_state = _sov is not None
    # the U.C.C. 1-308 reservation cites the FORUM's enactment, derived from its own code [forum_profile];
    # federal / underived forums keep the filer's home enactment (13 Pa.C.S. § 1308)
    _r = _fp.field(_court, "reservation_1308") if _sov else {}
    _resv = f"{_r['cite']} / U.C.C. § 1-308" if _r.get("cite") else "13 Pa.C.S. § 1308 / U.C.C. § 1-308"
    _appear = ("Appearing personally" if _is_state
               else "Appearing personally under 28 U.S.C. § 1654")
    # USER-LOCKED form: conformed "/s/" line, NO blank underscore line in the signature
    # area (form fill-in underscores on IFP/cover-sheet pages are legitimate and are not
    # emitted here). Re-adding an underscore rule here is a regression.
    lines = ["", "Respectfully submitted,", "", f"/s/ {name}",
             f"{name}, sui juris",
             f"All rights reserved without prejudice; {_resv}",
             _appear]
    lines += addr
    lines.append(f"Dated: {d}")
    return "\n\n".join(lines)

def verification_block(court=None, dated=None):
    """Pa.R.C.P. 1024 verification -- REQUIRED for any pleading averring facts.

    Enacted text, 231 Pa. Code Ch. 1000, Rule 1024(a): "Every pleading containing an
    averment of fact not appearing of record in the action or containing a denial of
    fact shall state that the averment or denial is true upon the signer's personal
    knowledge or information and belief and shall be verified." 1024(c) requires the
    verification be made by a party filing the pleading.

    A response to preliminary objections that answers factual averments is such a
    pleading, and an unverified pleading is strikeable -- the exact procedural defect
    an opponent gets for free. The form follows the Code's own verification language
    (231 Pa. Code Chs. 200/1920/1930), including the 18 Pa.C.S. 4904 unsworn-
    falsification reference that PA practice attaches.

    State courts only: the federal analogue is 28 U.S.C. 1746, whose wording differs,
    so a PA-shaped verification must not be emitted into a federal filing.
    """
    name, _ = _sig_address()
    d = dated or datetime.date.today().strftime("%B %d, %Y")
    _court = (court or "").upper()
    if not (_court.startswith("PA_") or "PHILA" in _court):
        return ""
    return ("\n\n## VERIFICATION\n\n"
            f"I, {name}, sui juris, verify that the statements made in the foregoing "
            "are true and correct upon my personal knowledge, or upon information and "
            "belief as to matters not within my personal knowledge. I understand that "
            "false statements herein are made subject to the penalties of 18 Pa.C.S. "
            "§ 4904, relating to unsworn falsification to authorities.\n\n"
            f"/s/ {name}\n\n"
            f"{name}, sui juris\n\nDated: {d}")


def certificate_of_service(method="first-class United States Mail, postage prepaid", dated=None,
                           recipients=None, described=None):
    """recipients: list of names (or 'Name — address' lines). When given, each is NAMED —
    the gate rejects 'all parties of record' because a mailing can't be checked against it."""
    name, _ = _sig_address()
    d = dated or datetime.date.today().strftime("%B %d, %Y")
    if recipients:
        what = described or "the foregoing"
        lines = "\n".join(f"{i}. {r}" for i, r in enumerate(recipients, 1))
        return ("\n\n## CERTIFICATE OF SERVICE\n\n"
                f"I, {name}, appearing personally, certify that on {d} I served a true and correct copy of "
                f"{what} by {method} on each of the following:\n\n{lines}\n\n"
                f"/s/ {name}\n\n{name}, sui juris\n\nDated: {d}")
    return ("\n\n## CERTIFICATE OF SERVICE\n\n"
            f"I, {name}, appearing personally, hereby certify "
            "that on the date set forth below, a true and correct copy of the foregoing "
            f"was served on all parties of record by {method}, at the addresses on file "
            "with the Court.\n\n" + f"/s/ {name}\n\n"
            f"{name}, sui juris\n\nDated: {d}")

def replace_cos(md, recipients, described=None, method="first-class United States Mail, postage prepaid"):
    """Swap whatever COS the body carries for a NAMED one (class-level: any lane, any court)."""
    i = md.find("## CERTIFICATE OF SERVICE")
    if i < 0:
        return md + certificate_of_service(method, recipients=recipients, described=described)
    j = md.find("\n## ", i + 5)
    tail = md[j:] if j > 0 else ""
    return md[:i].rstrip() + certificate_of_service(method, recipients=recipients, described=described) + tail


def service_recipients(stub_path):
    """Proof of service must name each person served and the address used (Fed. R. App. P. 25(d)(1)(B); Fed. R. Civ.
    P. 5). CLASS organ = pipeline.service_roster: names from m0's serve list in the stub's bundle ∪ the CA3 Clerk-order
    cc lists (CASE_TIMELINE); addresses from letterstream_final manifests (what is actually mailed), else the district
    docket's attorney blocks, else 'address NOT IN RECORD' (kept + reported, never dropped). Each row's source is kept
    in <stub dir>/SERVICE_ROSTER_<appeal>.json and as an HTML comment in the draft .md (stripped from the .docx).
    The clerk/chambers are the filing office, not parties served; the filer is never his own recipient."""
    from pipeline import m1_screen as _s
    from pipeline import service_roster as _sr
    p = Path(stub_path).resolve()
    _c, appno = _s.caption_of(p.read_text(encoding="utf-8", errors="replace"))
    case_dir = next((d for d in p.parents if (d / "CASE_TIMELINE.json").exists() or (d / "CASE_FACTS.md").exists()), None)
    if case_dir is None:
        print("  [service] no case dir (CASE_TIMELINE.json) above the stub — certificate names no recipients (gate will flag)")
        return None
    self_name, _ = _sig_address()
    r = _sr.roster_rows(p, case_dir, ROOT / "efiling" / "letterstream_final", appno, self_name)
    if not r["rows"]:
        print("  [service] roster empty — certificate names no recipients (gate will flag)")
        return None
    side = p.parent / f"SERVICE_ROSTER_{appno or 'case'}.json"
    side.write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    for d in r["detail"]:
        print(f"  [service] {d['display']} | {d['address']} | name: {d['name_source']} | address: {d['address_source']}")
    for u in r["unresolved"]:
        print(f"  [service] UNRESOLVED cc entry '{u['entry']}' ({u['source']}): {u['why']}")
    print(f"  [service] {len(r['rows'])} recipients -> {side.name}")
    return [f"{row} <!-- name: {d['name_source']}; address: {d['address_source']} -->" for row, d in zip(r["rows"], r["detail"])]


def serve_list_with_addresses(serve_list_path, final_dir=None):
    """Names from serve_list.txt; address appended when a letterstream_final manifest knows it."""
    import json as _json
    names = [l.strip() for l in Path(serve_list_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    addr = {}
    for m in (Path(final_dir).glob("*/manifest.json") if final_dir else []):
        try:
            d = _json.loads(m.read_text(encoding="utf-8")); a = d.get("address", {})
            addr[d.get("recipient", "")] = ", ".join(x for x in (a.get("line1"), a.get("line2"), a.get("city"),
                                                            " ".join(y for y in (a.get("state"), a.get("zip")) if y)) if x)
        except Exception:
            pass
    out = []
    for n in names:
        full = next((k for k in addr if n.split()[-1] in k and (n.split()[0] in k)), None)
        out.append(f"{full} — {addr[full]}" if full else n)
    return out


# A RESPONSE does not ask the court to grant "the Motion" — the motion is the
# opponent's. The prayer must match the filing's posture or it asks the court to
# grant the very relief being opposed.
OPPOSITION_TYPES = {
    "opposition", "response", "response_in_opposition", "answer_to_preliminary_objections",
    "06_opposition_prelim_objections", "opposition_prelim_objections", "reply_brief",
}



# What a response lane is OPPOSING, derived from the filing type — no case-specific
# strings, so a new court or motion type needs no code change.
# (match_keys, label, is_plural, overruled_not_denied)
_INSTRUMENTS = (
    (("preliminary objection", "prelim objection", "prelimobjection"),
     "the Preliminary Objections", True, True),
    (("demurrer",), "the Demurrer", False, True),
    (("motion to dismiss", "motiontodismiss"), "the Motion to Dismiss", False, False),
    (("summary judgment", "summaryjudgment"), "the Motion for Summary Judgment", False, False),
    (("motion to strike",), "the Motion to Strike", False, False),
    (("motion to compel",), "the Motion to Compel", False, False),
    (("petition",), "the Petition", False, False),
)


def _opposed_instrument(filing_type: str):
    """-> (label, is_plural, overruled) for whatever this lane opposes."""
    ft = (filing_type or "").lower().replace("_", " ")
    ftc = ft.replace(" ", "")
    for keys, label, plural, overruled in _INSTRUMENTS:
        for k in keys:
            if k in ft or k.replace(" ", "") in ftc:
                return label, plural, overruled
    return "the Motion", False, False


# AFFIRMATIVE instruments — filings that are not responses to anything. Added 2026-09-29
# (Lippi/0000 EDA 2026). An opposition is named by what it opposes; an affirmative instrument
# is named by the RULE THAT AUTHORIZES IT, because in the appellate courts the rule number is
# what makes the paper cognizable. Pa.R.A.P. 123 is the generic application vehicle: any relief
# not covered by a specific rule is sought by application, so the title must say both the
# authorizing rule and the act demanded.
# (match_keys, TITLE)
_AFFIRMATIVE = (
    (("record_transmission_enforcement_1935", "transmission of the record", "transmit the record",
      "delinquent record", "1935"),
     "APPLICATION FOR RELIEF UNDER Pa.R.A.P. 123 TO ENFORCE Pa.R.A.P. 1935 "
     "AND COMPEL TRANSMISSION OF THE RECORD ON APPEAL"),
    (("application_for_relief", "rap_123", "application for relief"),
     "APPLICATION FOR RELIEF UNDER Pa.R.A.P. 123"),
)


def _affirmative_title(filing_type: str) -> str:
    ft = (filing_type or "").lower().replace("_", " ")
    ftc = ft.replace(" ", "")
    for keys, title in _AFFIRMATIVE:
        for k in keys:
            if k.lower() in ft or k.lower().replace("_", "").replace(" ", "") in ftc:
                return title
    return ""


def instrument_title(case_id: str, filing_type: str, court: str = "") -> str:
    """The filing's real name, derived from what it opposes -- never the file stem.
    'PLAINTIFF'S RESPONSE IN OPPOSITION TO DEFENDANT'S PRELIMINARY OBJECTIONS'. Role words come
    from the registry caption (who we are), the instrument from _INSTRUMENTS (what they filed).
    Returns '' for a non-opposition so callers keep their own title."""
    # Affirmative instruments are named by their authorizing rule, checked first: a
    # record-enforcement application is not an opposition and must not fall through to ''.
    aff = _affirmative_title(filing_type)
    if aff:
        # A supplement to an already-pending application says so, so the court dockets it to
        # the live application instead of opening a second one.
        ftl = (filing_type or "").lower()
        if "supplement" in ftl:
            return "SUPPLEMENT TO " + aff
        return aff
    if _posture_of(filing_type) != "opposition":
        return ""
    label, plural, _ = _opposed_instrument(filing_type)
    inst = re.sub(r"^the\s+", "", label).upper()
    reg = registry_caption_ctx(case_id) or {}
    cap = reg.get("caption") or {}
    defs = cap.get("defendants") or []
    theirs = "DEFENDANTS'" if isinstance(defs, list) and len(defs) > 1 else "DEFENDANT'S"
    if re.search(r"sur[-_ ]?reply", (filing_type or "").lower()):
        return f"PLAINTIFF'S SUR-REPLY BRIEF IN OPPOSITION TO {theirs} {inst}"
    return f"PLAINTIFF'S RESPONSE IN OPPOSITION TO {theirs} {inst}"


def adverse_control_no(case_id: str, filing_type: str) -> str:
    """Control number of the instrument this lane opposes, read from CASE_TIMELINE.json.
    Phila.Civ.R. *208.3: the Motion Clerk's Control Number 'must be used on all Responses'.
    Match the timeline event to the opposed instrument by name; take the most recent. Never
    typed by hand -- an absent number stays absent and the caption simply omits the line."""
    if _posture_of(filing_type) != "opposition":
        return ""
    label, _, _ = _opposed_instrument(filing_type)
    if label == "the Motion":
        # generic fallback = instrument unknown; a bare 'motion' would match any motion in
        # the timeline (an IFP petition's number went onto a PO response this way). No match
        # is safer than the wrong number: the caption then omits the line and the log says so.
        return ""
    key = re.sub(r"^the\s+", "", label).lower().rstrip("s")
    tl = _case_root(case_id) / "CASE_TIMELINE.json"
    if not tl.is_file():
        return ""
    try:
        d = json.loads(tl.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    # the file carries several list keys ('timeline' is a 2-item header; 'entries' holds the
    # events) -- take every dict-bearing list, so no key name is assumed
    ev = d if isinstance(d, list) else [e for v in d.values() if isinstance(v, list) for e in v]
    hits = []
    for e in ev:
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k, "")) for k in ("event", "docket_ref", "description", "summary"))
        if key not in blob.lower():
            continue
        m = re.search(r"Control\s*(?:No\.?|Number|#)?\s*:?\s*(\d{6,})", blob, re.I)
        if m:
            hits.append((str(e.get("date", "")), m.group(1)))
    if not hits:
        return ""
    hits.sort()
    return hits[-1][1]


def _posture_of(filing_type: str) -> str:
    ft = (filing_type or "").lower()
    if "screen" in ft:
        return "screen"                # appellate court screens the appeal itself: no movant, nothing to deny
    if ft in OPPOSITION_TYPES or "opposition" in ft or "response" in ft or re.search(r"sur[-_ ]?reply", ft):
        return "opposition"            # a sur-reply answers the movant's reply: still our opposition to their instrument
    return "motion"


def _relief_from_stub(case_id: str, instrument_hint: str = "") -> list:
    """Read m0's OWN relief section out of the stub. m0 decides what to ask for;
    this only transcribes it. Returns [] when m0 wrote none -- never invents relief.
    """
    root = _case_root(case_id) / "drafts"
    if not root.exists():
        return []
    stem = (instrument_hint or "").strip()
    cands = sorted(root.rglob(f"{stem}.md")) if stem else []
    if not cands:
        cands = sorted(root.rglob("*opposition*.md")) + sorted(root.rglob("*response*.md"))
    for p in cands:
        if "_M1_DRAFT" in p.name:
            continue
        try:
            txt = p.read_text(errors="replace")
        except Exception:
            continue
        m = re.search(r"(?mi)^#+\s*[A-Z]?\.?\s*Relief requested\s*$(.*?)(?=^#+\s|\Z)",
                      txt, re.S)
        if not m:
            continue
        blob = " ".join(m.group(1).split())
        if not blob:
            continue
        # m0 writes prose sentences; each becomes one numbered request.
        parts = [x.strip() for x in re.split(r"(?<=[.;])\s+(?=[A-Z])", blob) if x.strip()]
        out = []
        for x in parts:
            x = x.rstrip(".").strip()
            if len(x) < 8:
                continue
            # A prayer ASKS; it does not assert. m0's relief section mixes requests
            # ("Overrule the objections") with the propositions supporting them
            # ("Dismissal with prejudice is not available where..."). Only the
            # requests belong under WHEREFORE -- the propositions are argued above.
            # Agnostic test: a declarative clause whose subject takes a stated verb
            # is a proposition; an imperative opening with a bare verb is a request.
            # Agnostic test, no cause-of-action vocabulary: a REQUEST opens with a
            # bare imperative verb and carries no stated verb in its opening clause
            # ("Overrule the objections", "Grant leave to amend"). A PROPOSITION
            # predicates something ("Dismissal ... is not available", "Amendment
            # cannot be denied"). Look for a finite verb before the first comma.
            if re.search(r"(?i)\b(?:is|are|was|were|has|have|had|cannot|can not|"
                         r"may not|must|should|would|does not|do not|did not)\b",
                         x.split(",")[0]):
                continue
            out.append(x)
        if out:
            return out
    return []


def _stub_for(case_id: str, instrument_hint: str = "") -> str:
    """m0's stub for this filing. Every anatomy section below is transcribed from it,
    so the draft costs no inference and says only what m0 already derived."""
    d = _case_root(case_id) / "drafts"
    if not d.exists():
        return ""
    stem = (instrument_hint or "").strip()
    cands = sorted(d.rglob(f"{stem}.md")) if stem else []
    if not cands:
        cands = sorted(d.rglob("*opposition*.md")) + sorted(d.rglob("*response*.md"))
    for p in cands:
        if "_M1_DRAFT" in p.name:
            continue
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return ""


# generator placeholders ("(m0 fills ...)") are a stub's empty slots; they never plead.
_PLACEHOLDER = re.compile(r"\(m0 fills[^)]*\)", re.I)


def _operative_facts(case_id: str, limit: int = 2200) -> str:
    """CASE_FACTS § OPERATIVE FACTS as filing prose: m0's bold-led narrative paragraphs,
    in record order, without the auto-appended pending-integration ledger."""
    cf = _case_root(case_id) / "CASE_FACTS.md"
    try:
        t = cf.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(r"(?ms)^## OPERATIVE FACTS[^\n]*\n(.*?)(?=^## |\Z)", t)
    if not m:
        return ""
    block = m.group(1)
    # the EVIDENCE_ASSERT ledger is a to-do list for m0, not narrative
    block = re.split(r"(?m)^### RECORD FACTS PENDING", block)[0]
    paras = []
    for para in re.split(r"\n\s*\n", block):
        para = " ".join(para.split())
        if not para or para.startswith(("|", ">", "```", "- `")):
            continue
        # "**Policy and Parties.** text" -> "text" (the label is m0's index, not prose)
        para = re.sub(r"^\*\*[^*]{2,60}\*\*\s*", "", para)
        para = re.sub(r"\*\*(.+?)\*\*", r"\1", para)
        if len(para) >= 40:
            paras.append(para)
    txt = " ".join(paras)
    if limit and len(txt) > limit:
        cut = txt.rfind(". ", 0, limit)
        txt = txt[:cut + 1] if cut > 0 else txt[:limit]
    return txt.strip()


def _stub_section(stub: str, *titles) -> str:
    """Body of the first '## <title>' section that matches any of `titles`."""
    for t in titles:
        m = re.search(r"(?mi)^##+\s*" + t + r"[^\n]*\n(.*?)(?=^##\s|\Z)",
                      stub or "", re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _prose(block: str, limit: int = 0) -> str:
    """Stub prose -> filing prose: drop m0's drafting directives and bullets."""
    lines = []
    for ln in (block or "").splitlines():
        t = ln.strip()
        if not t or t.startswith(("#", "|", ">", "```")):
            continue
        lines.append(re.sub(r"^[-*\u2022]\s*", "", t))
    # m0 wraps its prose, so a drafting directive can begin mid-line. Rejoin first,
    # then drop whole SENTENCES: dropping lines guts the middle of a sentence and
    # leaves fragments in a filed document.
    txt = re.sub(r"\s+", " ", " ".join(lines)).strip()
    _DIRECTIVE = re.compile(
        r"(?i)^(?:lead with|build the table|do not narrate|verify before|weave per|"
        r"state the rule exactly|counter-premise|"
        r"draft |use the |cite the |note the |remember )")
    keep = []
    for x in re.split(r"(?<=[.!?])\s+(?=[A-Z\u201c\"(])", txt):
        st = x.strip()
        if not st or _DIRECTIVE.match(st):
            continue
        # "Fork to plead X, in which case..." / "Defeat the premise and..." carry the
        # substantive argument after the directive verb -- strip the verb, keep the
        # sentence, so the filing does not lose the pleading's alternative theory.
        st = re.sub(r"(?i)^fork to plead\s+(?:that\s+)?(?=in the alternative)", "Plead ", st)
        st = re.sub(r"(?i)^fork to plead\s+(?:that\s+)?", "Either ", st)
        st = re.sub(r"(?i)^defeat the premise and\s+", "Once the premise fails, ", st)
        if st and st[0].islower():
            st = st[0].upper() + st[1:]
        keep.append(st)
    txt = " ".join(keep)
    if limit and len(txt) > limit:
        cut = txt.rfind(". ", 0, limit)
        txt = txt[:cut + 1] if cut > 80 else txt[:limit].rstrip() + "."
    return txt


def _style_profile(court: str, kind: str = "brief_or_opposition") -> dict:
    """Per-court section contract from m1_style_profiles.json (DATA, never hardcoded).

    The profile's own _doc says section order is "Data-driven -- never hardcoded in code",
    yet anatomy_sections() took `court` and ignored it, so EVERY court got the federal
    anatomy (INTRODUCTION / STATEMENT OF THE CASE / STANDARD OF REVIEW). In the Court of
    Common Pleas that is a RULE violation: Phila.Civ.R. *210 mandates five separately and
    distinctly titled sections in a fixed order, and *1028(c)(1)(a) requires the brief.
    """
    try:
        import json as _j, os as _o
        from pathlib import Path as _P
        try:
            from pipeline.court_hierarchy import canonical_court_id as _cci
            court = _cci(court) or court
        except Exception:
            pass
        cands = []
        _env = _o.environ.get("M1_STYLE_PROFILES")
        if _env:
            cands.append(_P(_env))
        # repo root relative to THIS file, then CWD -- a sandboxed copy of this module
        # must still find the live profile unless M1_STYLE_PROFILES overrides it.
        cands.append(_P(__file__).resolve().parent.parent / "output" / "audit"
                     / "m1_style_profiles.json")
        cands.append(_P.cwd() / "output" / "audit" / "m1_style_profiles.json")
        for cand in cands:
            if cand.is_file():
                prof = _j.loads(cand.read_text(encoding="utf-8")) or {}
                return ((prof.get(court) or {}).get(kind) or {})
    except Exception:
        pass
    return {}


def _mandatory_heads(court: str) -> list:
    """[(HEADING, rule, requires)] the court's rules REQUIRE, in rule order."""
    ms = _style_profile(court).get("mandatory_sections") or []
    return [(m.get("heading", ""), m.get("rule", ""), m.get("requires", "")) for m in ms
            if m.get("heading")]


# Acronyms that are proper nouns in a filing and must stay capitalized.
_KEEP_CAPS = {"UTPCPL", "UCC", "ECF", "MVFRL", "DOI", "ABC"}


def _pluralize_verb(subject: str, rest: str) -> str:
    """"gist of the action" + "bar these counts" -> "bars these counts".

    Inverting "X does not BAR" to the affirmative needs subject-verb agreement, or the
    question reads "whether gist of the action bar these counts".
    """
    parts = rest.split(None, 1)
    if not parts:
        return rest
    verb, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    plural_subject = bool(re.search(r"(?i)\b(counts|claims|they|objections|parties)\s*$",
                                    subject.strip()))
    if not plural_subject and re.match(r"(?i)^[a-z]+$", verb) and not verb.endswith("s"):
        verb = verb + ("es" if re.search(r"(?:s|sh|ch|x|z|o)$", verb) else "s")
    return (verb + (" " + tail if tail else "")).strip()


def _questions_from_matrix(case_id: str) -> str:
    """Phila.Civ.R. *210(2) questions derived from the movant's own objection headings.

    The matrix (output/audit/<slug>_rebuttal_matrix.json) lists every heading of the
    adverse filing. Each heading is the movant's proposition; the question is that
    proposition put neutrally, and the advocated answer in an opposition is No.
    """
    mp = ROOT / "output" / "audit" / f"{case_id}_rebuttal_matrix.json"
    try:
        mx = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return ""
    heads = []
    for _b, info in (mx.get("briefs") or {}).items():
        heads += list(info.get("headings_found") or [])
    qs, seen = [], set()
    for h in heads:
        h = " ".join(str(h).split()).rstrip(". ")
        if len(h) < 12:
            continue
        # neutralise the movant's imperative ("should be dismissed because ...") into
        # the proposition it asserts; the trailing dependent clause is their reasoning,
        # not the issue.
        prop = re.sub(r"(?i)\s+(?:because|as it|since|where)\b.*$", "", h).strip()
        prop = re.sub(r"(?i)^(?:the\s+)?(?:court\s+)?should\s+", "", prop)
        prop = re.sub(r"(?i)\s+should\s+be\s+dismissed$", " must be dismissed", prop)
        if prop[:1].isupper() and not re.match(
                r"^(?:[A-Z]{2,}|Pa\.|Defendant|Plaintiff|Count|Progressive|The Pennsylvania)",
                prop):
            prop = prop[0].lower() + prop[1:]
        key = prop.lower()
        if key in seen:
            continue
        seen.add(key)
        qs.append(f"{len(qs) + 1}. Whether {prop}?  Answer: No.")
    return "  ".join(qs)


def _questions_from_mirrors(stub: str) -> str:
    """Collect each MIRROR's closing two-part question to the court (deduped, in stub order), numbered.
    A close with no (a)/(b) is not a question and is skipped; the skeptic already grades those OPEN."""
    seen, qs = set(), []
    for m in re.finditer(r"(?is)the court is asked to state\s*\(a\)(.+?)\(b\)(.+?)(?:instead)?[.?](?=\s|$)", stub or ""):
        a = re.sub(r"(?i),?\s*and\s*$", "", re.sub(r"\s+", " ", m.group(1)).strip(" ,;")).strip(" ,;")
        b = re.sub(r"\s+", " ", m.group(2)).strip(" ,;")
        key = (a.lower()[:80], b.lower()[:80])
        if not a or not b or key in seen:
            continue
        seen.add(key)
        qs.append(f"{len(qs)+1}. The Court is asked to state (a) {a}, and (b) {b} instead.")
    return "\n".join(qs)


def _questions_involved(stub: str, case_id: str = "") -> str:
    """Phila.Civ.R. *210(2): each issue in QUESTION form, yes/no, + the answer advocated.

    Built from the stub's own SECTION MAP contentions -- m0 already framed one contention
    per count, which is exactly one question per count. Deterministic: the question is the
    contention restated interrogatively, and the advocated answer is always the movant's
    objection failing, because that is what an opposition contends.
    """
    qs = []
    for m in re.finditer(r"(?m)^###\s+([A-G])\.\s+(.+?)\s*$", stub or ""):
        head = m.group(2).strip()
        if re.match(r"(?i)^(their authorities|what defendant conceded|which counts|relief)",
                    head):
            continue
        # m0's stub heads read "Counts II & VI -- gist of the action does not bar these
        # counts": a COUNT SCOPE, an em-dash, then the CONTENTION. *210(2) wants one
        # grammatical question carrying the factual context, so split on the dash and
        # rebuild -- stapling "Whether" onto the whole head yields "Whether counts II & VI
        # -- gist of the action does not bar these counts?", which is not English.
        head = re.sub(r"\s*\((?:lead with this|build the table[^)]*|do not narrate[^)]*)\)\s*$",
                      "", head, flags=re.I).strip().rstrip(".")
        scope, contention = "", head
        mm = re.match(r"^(.*?)\s*[\u2014\u2013-]{1,2}\s*(.+)$", head)
        if mm and re.match(r"(?i)^counts?\b", mm.group(1).strip()):
            scope, contention = mm.group(1).strip(), mm.group(2).strip()
        contention = contention.rstrip(".").strip()
        if not contention:
            continue
        # m0 writes the contention in ADVOCACY voice ("the UTPCPL objection dies on the
        # ENACTED TEXT", "gist of the action does not bar these counts"). *210(2) wants a
        # NEUTRAL issue whose advocated answer is stated separately -- and a negatively
        # framed question ("whether X does not bar") makes "Yes" affirm a negative, which
        # reads as a concession. Invert to the objection's own proposition and advocate NO.
        neg = re.match(r"(?i)^(.*?)\s+do(?:es)?\s+not\s+(.+)$", contention)
        answer = "Yes"
        if neg:
            subject, rest = neg.group(1).strip(), neg.group(2).strip()
            contention, answer = f"{subject} {_pluralize_verb(subject, rest)}", "No"
        else:
            m2 = re.match(r"(?i)^(the .+? objection)\s+(?:dies|fails|collapses)\b.*$",
                          contention)
            if m2:
                contention, answer = f"{m2.group(1)} is well founded", "No"
        contention = re.sub(r"\b([A-Z]{4,})\b",
                            lambda x: x.group(1) if x.group(1) in _KEEP_CAPS
                            else x.group(1).lower(), contention)
        if contention[:1].isupper() and not re.match(
                r"^(?:[A-Z]{2,}|Pa\.|Defendant|Plaintiff|Count)", contention):
            contention = contention[0].lower() + contention[1:]
        q = f"Whether {contention}"
        if scope:
            q += f", as pleaded in {scope}"
        qs.append(f"{len(qs) + 1}. {q}?  Answer: {answer}.")
    return "  ".join(qs)


def anatomy_sections(case_id: str, instrument_hint: str = "", court: str = "") -> list:
    """Opposition anatomy, transcribed from m0's stub with zero LLM calls.

    Returns [(heading, body), ...] in filing order. A section m0 wrote nothing for is
    omitted rather than invented -- an empty heading in a filing is worse than none.

    Section ORDER and section NAMES come from the court's style profile when it declares
    mandatory_sections (Phila.Civ.R. *210); otherwise the federal anatomy applies.
    """
    stub = _stub_for(case_id, instrument_hint)
    if not stub:
        return []
    secs = []

    heads = _mandatory_heads(court)
    if heads:
        return _anatomy_by_rule(stub, heads, case_id, instrument_hint, court)

    # 1. INTRODUCTION -- m0's "premise to attack" IS the introduction's thesis.
    prem = _prose(_stub_section(stub, r"THE PREMISE TO ATTACK FIRST"), 1400)
    if prem:
        secs.append(("INTRODUCTION", prem))

    # 2. STATEMENT OF THE CASE -- what was objected to, and what was not.
    scope = _prose(_stub_section(stub, r"WHAT DEFENDANT OBJECTED TO",
                                 r"RECORD SCOPE"), 1200)
    if scope:
        secs.append(("STATEMENT OF THE CASE", scope))

    # 3. STANDARD OF REVIEW -- built ONLY from rules m0 verified for this filing.
    #    No rule cited here that the stub does not already carry.
    rules = []
    for pat, txt in (
        (r"1028\(a\)[^.\n]{0,30}?\(4\)",
         "A preliminary objection in the nature of a demurrer, "
                              "Pa.R.C.P. 1028(a)(4), admits every well-pleaded fact in "
                              "the pleading it attacks and every inference fairly "
                              "deducible from those facts."),
        (r"1028\(a\)\(2\)", "An objection under Pa.R.C.P. 1028(a)(2) reaches the form "
                              "of a pleading, not the merits of the claim pleaded."),
        (r"1028\(b\)", "Pa.R.C.P. 1028(b) requires that all preliminary objections be "
                        "raised at one time."),
        (r"1032\(a\)", "A ground not so raised is waived. Pa.R.C.P. 1032(a)."),
        (r"1019\(a\)", "The material facts on which a cause of action is based must be "
                        "stated in a concise and summary form. Pa.R.C.P. 1019(a)."),
    ):
        if re.search(pat, stub):
            rules.append(txt)
    if rules:
        rules.append("The objections are therefore sustained only where the pleading, "
                     "so read, states no claim on which relief may be granted.")
        secs.append(("STANDARD OF REVIEW", " ".join(rules)))
    return secs


def _anatomy_by_rule(stub: str, heads: list, case_id: str, instrument_hint: str,
                     court: str) -> list:
    """Build the court's MANDATORY sections, in rule order, from m0's stub.

    ARGUMENT is deliberately returned EMPTY-BODIED: finish() appends the assembled
    rebuttal body under it, so filling it here would print the argument twice. RELIEF is
    likewise left to prayer_for_relief(), which already carries the WHEREFORE contract --
    but the HEADING must exist in *210 order, so it is emitted as a marker the assembler
    consumes rather than silently dropped.
    """
    out = []
    for heading, rule, _req in heads:
        H = heading.upper()
        if H.startswith("MATTER BEFORE"):
            # *210(1): the pleading before the court + the particular relief requested.
            what = _prose(_stub_section(stub, r"WHAT DEFENDANT OBJECTED TO",
                                        r"RECORD SCOPE"), 700)
            body = ("Before the Court are Defendant's Preliminary Objections. "
                    + (what or "") + " Plaintiff opposes the objections in their entirety "
                    "and requests that each be overruled.").strip()
            out.append((heading, re.sub(r"\s+", " ", body)))
        elif H.startswith("STATEMENT OF QUESTION") or "QUESTIONS INVOLVED" in H:
            # *210(2): one question per objection the movant raised. The matrix holds
            # those headings 1:1 (zero-waiver); the stub's section map holds the RULE's
            # section names, which are not issues.
            q = _questions_from_matrix(case_id) or _questions_involved(stub, case_id)
            # user-locked 2026-10-25: the movant's own questions come FIRST -- every MIRROR's close
            # "The Court is asked to state (a) why <rule> does not apply here, and (b) the law that gave
            # the Court authority to <act> instead" -- then the opponent's framing with our answers.
            mq = _questions_from_mirrors(stub)
            if mq:
                q = (mq + ("\n\nDefendant's questions as framed, and the answers of record:\n\n" + q if q else "")).strip()
            if q:
                out.append((heading, q))
        elif H == "FACTS":
            # *210(3): the facts, from m0's RECORD -- CASE_FACTS § OPERATIVE FACTS is the
            # narrative m0 wrote from intake. The stub's premise section is argument
            # scaffolding, not facts, and when the generator left its placeholder there
            # the placeholder printed as the section.
            f = _operative_facts(case_id, 2200)
            if not f:
                f = _prose(_stub_section(stub, r"THE PREMISE TO ATTACK FIRST"), 1400)
            if f and not _PLACEHOLDER.search(f):
                out.append((heading, f))
        elif H == "ARGUMENT":
            out.append((heading, ""))          # body appended by finish()
        elif H == "RELIEF":
            out.append((heading, "__PRAYER__"))  # replaced by prayer_for_relief()
        else:
            out.append((heading, ""))
    return out


def anatomy_md(case_id: str, instrument_hint: str = "", court: str = "") -> str:
    """Headings + bodies that PRECEDE the argument body.

    A rule-ordered anatomy stops at ARGUMENT: finish() emits "## ARGUMENT" itself and
    appends the assembled body under it, and RELIEF is emitted by prayer_for_relief().
    Printing those markers here would duplicate the heading and leak "__PRAYER__".
    """
    secs = anatomy_sections(case_id, instrument_hint, court)
    if not secs:
        return ""
    out = []
    for head, body in secs:
        if head.upper() in ("ARGUMENT", "RELIEF") or body == "__PRAYER__":
            break
        out.append(f"## {head}")
        if body:
            out.append(body)
    return "\n\n".join(out)


def prayer_for_relief(case_id: str = "", instrument_hint: str = "",
                      relief_lines=None, court: str = "") -> str:
    """PRAYER FOR RELIEF -- anatomy step 6. Without it the filing asks the court for
    nothing, and the [PROPOSED] ORDER has no request behind it.

    Relief comes from m0's stub section (m0 decides ALL outgoing legal content); the
    generic fallback is used only when m0 wrote no relief section.
    """
    name, _ = _sig_address()
    lines = list(relief_lines or []) or _relief_from_stub(case_id, instrument_hint)
    if lines and court:
        lines = [forum_relief_line(ln, court) for ln in lines]   # S-relief-forum-vocab (idempotent)
    if not lines:
        # reuse the SAME organ the proposed order uses, so the prayer and the order
        # name the same instrument and agree in number.
        opposed, plural, overruled = _opposed_instrument(instrument_hint)
        verb = "Overrule" if overruled else "Deny"
        lines = [f"{verb} {opposed} in {'their' if plural else 'its'} entirety"]
    out = ["", "## PRAYER FOR RELIEF", "",
           f"WHEREFORE, Plaintiff {name}, sui juris, respectfully requests that this "
           f"Honorable Court:"]
    for i, ln in enumerate(lines, 1):
        ln = ln[0].upper() + ln[1:] if ln else ln
        out.append(f"{i}. {ln};")
    out.append(f"{len(lines) + 1}. Grant such other and further relief as this Court "
               f"deems just and proper.")
    return "\n\n".join(out)


_RESERVATION_RULES = {
    # forum -> (waiver rule for the MOVANT's unraised grounds, deemed-admission rule, one-time rule)
    # Each is an enacted rule the corpus carries verbatim (231 Pa. Code / Fed. R. Civ. P.). The
    # clause cites the forum's own rules; a forum with no entry gets the generic clause.
    "PA_CCP": ("Pa.R.C.P. 1032(a)", "Pa.R.C.P. 1029(b)", "Pa.R.C.P. 1028(b)"),
    "FED":    ("Fed. R. Civ. P. 12(g)(2) and 12(h)(1)", "Fed. R. Civ. P. 8(b)(6)", "Fed. R. Civ. P. 12(g)(2)"),
}


def reservation_block(instrument_hint: str = "", court: str = "") -> str:
    """RESERVATION OF RIGHTS / NON-WAIVER -- rides directly below RELIEF in every opposition.

    Symmetry the user has locked: the movant's silence is the waiver, never the respondent's.
    (1) Nothing in this response concedes any objection, ground or characterization not
    expressly addressed; (2) the respondent stands on the pleading as filed and waives no
    claim, count, averment or right; (3) the movant's grounds not raised in its one filing
    are waived under the forum's own rule, and the averments it did not engage stand
    admitted. No leave-to-amend language: amend-vs-stand is the user's election, and the
    election here is to stand."""
    c = (court or "").upper()
    key = "PA_CCP" if c.startswith("PA_") else ("FED" if c else "")
    waiver, admit, one_time = _RESERVATION_RULES.get(key, ("the applicable rule of waiver",
                                                            "the applicable rule of deemed admission",
                                                            "the applicable rule"))
    opposed, plural, _ = _opposed_instrument(instrument_hint)
    them = "they" if plural else "it"
    return "\n\n".join([
        "", "## RESERVATION OF RIGHTS", "",
        f"Plaintiff responds to {opposed} as {them} {'are' if plural else 'is'} written. Nothing in this "
        f"response, and no omission from it, concedes any objection, ground, characterization of the "
        f"pleading, or statement of the law that is not expressly addressed above; each is denied. "
        f"Plaintiff stands on the Complaint as filed and waives no claim, count, averment, right, or "
        f"remedy, at law or in equity, all of which are expressly reserved.",
        f"Any ground the movant did not raise in {opposed} is waived by the movant ({waiver}; {one_time}). "
        f"Any averment of the Complaint that {opposed} {'do' if plural else 'does'} not specifically engage "
        f"stands admitted for purposes of {opposed} ({admit}). Waiver is not presumed against Plaintiff "
        f"and is not to be inferred from silence, brevity, or the order in which points are addressed.",
    ])


# S-order-decretal (2026-10-25): a [PROPOSED] ORDER speaks in the court's voice ("the Order is VACATED"),
# not the movant's ("Vacate the Order"). The relief lines are the stub's prayer, written imperatively; the
# order renders them decretally. Only the leading imperative verbs below are rewritten; any other line
# passes through untouched, and the transform runs ONLY on the affirmative-motion branch of proposed_order()
# (opposition orders already speak decretally: "the Preliminary Objections are OVERRULED").
_TRAILING_CITE_RX = re.compile(
    r"\.\s+((?:\d+\s+)?(?:Pa\.\s?C\.S\.|Pa\.\s?R\.\s?C\.\s?P\.|Pa\.\s?R\.\s?A\.\s?P\.|U\.S\.C\.|Fed\.\s?R\.\s?(?:Civ|App)\.\s?P\.)"
    r"[^.]*?)\.?\s*$")


def _is_plural_subject(subj: str) -> bool:
    s = subj.strip()
    return bool(re.search(r"\b(?:Counts|Objections|Motions|Claims|Paragraphs|Exhibits|Defendants|Plaintiffs)\b", s)
                or re.search(r"\b[IVX]+\b\s*(?:,|and)\s*\b[IVX]+\b", s))


def _decretal(line: str) -> str:
    """Imperative prayer item -> decretal order item. Unknown verbs pass through unchanged."""
    s = (line or "").strip().rstrip(";")
    if not s:
        return s
    cite = ""
    m = _TRAILING_CITE_RX.search(s)
    if m:
        cite = m.group(1).strip()
        s = s[:m.start()].rstrip(".") + "."
    s = s.rstrip(".").strip()
    be = lambda subj: "are" if _is_plural_subject(subj) else "is"
    rules = [
        (r"^Vacate\s+(.+?)\s+insofar as\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} VACATED insofar as {g[1]}"),
        (r"^Vacate\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} VACATED"),
        (r"^Reinstate\s+(.+?)\s+and\s+(?:direct|order)\s+(.+?)\s+to\s+(.+)$",
         lambda g: f"{g[0]} {be(g[0])} REINSTATED, and {g[1]} shall {g[2]}"),
        (r"^Reinstate\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} REINSTATED"),
        (r"^Grant\s+leave\s+to\s+(.+)$", lambda g: f"leave to {g[0]} is GRANTED"),
        (r"^Grant\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} GRANTED"),
        (r"^(?:Direct|Order|Compel)\s+(.+?)\s+to\s+(.+)$", lambda g: f"{g[0]} shall {g[1]}"),
        (r"^Strike\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} STRICKEN"),
        (r"^Dismiss\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} DISMISSED"),
        (r"^Deny\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} DENIED"),
        (r"^Overrule\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} OVERRULED"),
        (r"^Enter\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} ENTERED"),
        (r"^Award\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} AWARDED"),
        (r"^Reconsider\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} RECONSIDERED"),
        (r"^Rescind\s+(.+)$", lambda g: f"{g[0]} {be(g[0])} RESCINDED"),
    ]
    out = None
    for rx, fn in rules:
        mm = re.match(rx, s, re.I)
        if mm:
            out = fn(mm.groups())
            break
    if out is None:
        return line.strip()            # unknown shape: leave the movant's words alone
    out = out[0].upper() + out[1:]
    out = out.rstrip(".") + "."
    return f"{out} {cite}." if cite else out


def proposed_order(relief_lines=None, filing_type=None, posture_type=None, court=None):
    posture = _posture_of(posture_type or filing_type)
    if relief_lines and court:
        relief_lines = [forum_relief_line(r, court) for r in relief_lines]   # S-relief-forum-vocab (idempotent)
    if posture == "opposition":
        # AGNOSTIC: the thing being opposed is named by the filing type itself, so the
        # order reads correctly for preliminary objections, a motion to dismiss, a
        # motion for summary judgment or anything else a lane is pointed at.
        opposed, plural, overruled = _opposed_instrument(filing_type)
        verb = "OVERRULED" if overruled else "DENIED"
        body = ["\n\n## [PROPOSED] ORDER\n",
                f"AND NOW, this _____ day of __________, 20____, upon consideration "
                f"of {opposed} and the Response in Opposition thereto, it is hereby "
                f"ORDERED that {opposed} {'are' if plural else 'is'} "
                f"{verb} in {'their' if plural else 'its'} entirety."]
        if relief_lines:
            body.append("It is further ORDERED that:")
            body += [f"{i}. {r}" for i, r in enumerate(relief_lines, 1)]
    else:
        body = ["\n\n## [PROPOSED] ORDER\n",
                "AND NOW, this _____ day of __________, 20____, upon consideration "
                "of the foregoing Motion, and any response thereto, it is hereby "
                "ORDERED that the Motion is GRANTED."]
        if relief_lines:
            body.append("It is further ORDERED that:")
            body += [f"{i}. {_decretal(r)}" for i, r in enumerate(relief_lines, 1)]   # S-order-decretal
    body += ["\nBY THE COURT:\n", "_________________________", "J."]
    return "\n\n".join(body)

def _mandatory_attachments(court: str, kind: str = "brief_or_opposition") -> list:
    """[{item,label,rule,requires}] the court's rules REQUIRE to be filed WITH this filing.
    DATA from rule_requirements (GROUND); empty when the forum declares none."""
    prof = _style_profile(court, kind)
    return [a for a in (prof.get("mandatory_attachments") or []) if isinstance(a, dict)]


def _answer_required(court: str, kind: str = "brief_or_opposition") -> dict:
    return dict(_style_profile(court, kind).get("answer_required") or {})


def _case_dir_for(case_id: str):
    """Case directory or None (thin wrapper over _case_root for the attachment organs)."""
    d = _case_root(case_id)
    return d if d.is_dir() else None


def _record_pleadings(case_id: str) -> list:
    """(label, path) of the case's own filed pleadings in the record, newest first, for
    the exhibits the rule names by kind ("the complaint, answer, and reply to new matter").
    Reads the case intake dir + packet; never invents a document."""
    import re as _re
    base = _case_dir_for(case_id)
    if base is None:
        return []
    out = []
    kinds = (("COMPLAINT", r"complaint"), ("ANSWER", r"\banswer"), ("REPLY TO NEW MATTER", r"reply.*new.?matter"))
    files = [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in (".pdf", ".docx")
             and ".bak" not in p.name and "_pre_" not in p.name and "DRAFT_PROOF" not in p.name]
    for label, rx in kinds:
        cands = [p for p in files if _re.search(rx, p.name, _re.I)
                 and not _re.search(r"exhibit|coversheet|cover_sheet|ifp|opposition|brief|prelim", p.name, _re.I)]
        if not cands:
            continue
        # prefer the as-filed / stamped copy, then the newest
        cands.sort(key=lambda p: (("asfiled" in p.name.lower() or "as_filed" in p.name.lower() or "filed" in p.name.lower()), p.stat().st_mtime), reverse=True)
        out.append((label, cands[0]))
    return out


def attachments_md(case_id: str, court: str, filing_type: str, instrument_hint: str = "") -> tuple:
    """Markdown for every attachment the forum's rule requires WITH this filing, plus the
    packet-file plan [(name, source_path|None)]. Pa. example (Phila.Civ.R. *1028(c)(4)):
    EXHIBITS "shall include the complaint ... marked as exhibits separately" and a PROPOSED
    ORDER "which shall contain no reference to the attorney proposing same". The brief
    (item (a)) is this document itself. AGNOSTIC: the items come from the contract, the
    documents from the record; nothing here names a court or a case.

    2026-09-27: the pipeline shipped a CCP opposition with zero exhibits while the brief
    accused the movant of exactly that omission under the same rule."""
    import re as _re
    atts = (_mandatory_attachments(court, filing_type) if filing_type else [])
    if not atts and "complaint" not in (filing_type or "").lower():   # a complaint never takes a brief's attachments
        atts = _mandatory_attachments(court)
    if not atts:
        return "", []
    md, plan = [], []
    for a in atts:
        label = (a.get("label") or a.get("item") or "").strip().upper()
        # the rule's operative words may sit in `item` or `requires` depending on how the
        # derive split them -- read both; never depend on the model's field choice
        req = " ".join((str(a.get("item") or "") + " " + str(a.get("requires") or "")).split())
        rule = str(a.get("rule") or "").strip()
        if not label:
            continue
        if _re.search(r"\bBRIEF\b|MEMORANDUM", label):
            continue  # this document
        if _re.search(r"\bORDER\b", label):
            continue  # proposed_order() already emits it; the no-attorney-reference rule is met there
        if _re.search(r"EXHIBIT", label):
            # which pleadings the rule names ("shall include the complaint, answer, and reply to new matter")
            named = [k for k in ("COMPLAINT", "ANSWER", "REPLY TO NEW MATTER") if k.lower() in req.lower()]
            have = dict(_record_pleadings(case_id))
            rows = []
            letter = ord("A")
            for k in named or ["COMPLAINT"]:
                src = have.get(k)
                if src is None:
                    continue  # not in the record = not a pleading in this case (no answer yet) -- not an omission
                rows.append((chr(letter), k.title(), src))
                letter += 1
            # the adverse filing being answered is "necessary or relevant to the disposition"
            adv = _adverse_filing_pdf(case_id, instrument_hint or filing_type)
            if adv is not None:
                rows.append((chr(letter), adv[0], adv[1]))
                letter += 1
            if not rows:
                continue
            md += ["", "## EXHIBITS", "",
                   f"Filed herewith and marked separately pursuant to {rule or 'the applicable rule'}:", "",
                   "| Exhibit | Document |", "|---|---|"]
            for ltr, title, src in rows:
                md.append(f"| {ltr} | {title} |")
                plan.append((f"EXHIBIT_{ltr}_{_re.sub(r'[^A-Za-z0-9]+', '_', title).strip('_')}{src.suffix.lower()}", src))
            continue
        # any other rule-mandated item we cannot build from the record: say so in the packet plan
        plan.append((f"{_re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_')}_REQUIRED_BY_{_re.sub(r'[^A-Za-z0-9]+', '_', rule).strip('_')}.TODO", None))
    return ("\n".join(md) if md else ""), plan


def _adverse_filing_pdf(case_id: str, instrument_hint: str = ""):
    """(title, path) of the movant's filing this document answers, from intake, or None."""
    import re as _re
    hint = (instrument_hint or "").lower()
    words = [w for w in _re.findall(r"[a-z]{4,}", hint) if w not in ("opposition", "brief", "response", "draft", "answer")]
    base = _case_dir_for(case_id)
    if base is None or not (base / "intake").is_dir():
        return None
    pdfs = [p for p in (base / "intake").rglob("*.pdf")]
    # match on the filename OR on the extracted text beside it (e-filing notices are
    # named by the clerk, e.g. "notice_of_an_e_filing_on.pdf", not by their content)
    def _text_of(p):
        for sfx in (".doctext.txt", ".ocr.txt", ".txt"):
            t = p.with_name(p.name + sfx)
            if t.is_file():
                try:
                    return t.read_text(encoding="utf-8", errors="ignore")[:6000].lower()
                except Exception:
                    return ""
        return ""
    def _hit(p):
        nm = p.name.lower()
        if words and all(w[:6] in nm for w in words[:2]):
            return 2
        tx = _text_of(p)
        if words and tx and all(w[:6] in tx for w in words[:2]):
            return 1
        return 0
    scored = [(_hit(p), p.stat().st_mtime, p) for p in pdfs]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    title = " ".join(w.title() for w in words) if words else "Adverse Filing"
    return (f"{title} (as filed)", scored[0][2])


def defects_table(case_id, max_rows=6):
    """DEFECTS IN THE RECORD table from the gated audit stream (scorecard data)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id.split(":", 1)[-1])
    cands = sorted((ROOT / "output" / "audit").glob(f"data_*{safe}*.json"))
    if not cands:
        return ""
    d = json.loads(cands[0].read_text())
    from collections import Counter
    cnt, meta = Counter(), {}
    for key in ("procedural_violations", "violations"):
        for v in d.get(key, []) or []:
            if not isinstance(v, dict): continue
            cat = v.get("category") or v.get("type") or v.get("violation_type")
            if not cat or cat == "AUTHORITY_BANK_GATE": continue
            n = v.get("count") if isinstance(v.get("count"), int) else 1
            cnt[cat] += n
            if cat not in meta:
                meta[cat] = (v.get("rule_citation") or v.get("rule") or "",
                             (v.get("explanation") or v.get("description") or "")[:100])
    if not cnt:
        return ""
    rows = ["\n\n### DEFECTS IN THE RECORD SUPPORTING THIS FILING\n",
            "The following defects from the case record support the relief sought, "
            "each established by the record and each independently sufficient:\n",
            "| # | Defect | Controlling Rule | Count | Record Basis |",
            "|---|---|---|---|---|"]
    for i, (cat, n) in enumerate(cnt.most_common(max_rows), 1):
        rule, basis = meta[cat]
        title = cat.replace("_", " ").title()
        rows.append(f"| {i} | {title} | {rule} | {n} | {basis} |")
    rows.append("\nSee the accompanying Scorecard Exhibits for the full ledger.")
    return "\n".join(rows)

def registry_caption_ctx(case_id: str) -> dict | None:
    """Resolve a filing caption from the case registry. SINGLE ORGAN for every m1 lane.

    The renderer (_build_caption_table) prints "PLAINTIFF NAME" / "Case No. ________"
    whenever ctx carries no caption, so a lane that cannot build one from a stub must
    resolve it here. Bridges the three identifier spaces in play:
        obsidian slug  example_v_insurer
        case_map       case_id "260100001"
        registry       key "phila_ccp_progressive" / canonical_id "phila_ccp:260100001"
    Returns a ctx fragment (caption/docket_no/court) or None when unresolvable.
    """
    import json as _json
    try:
        from pipeline.auto_reply import caption_for, _REG
    except Exception:
        return None
    key = case_id if case_id in _REG else None
    if key is None:
        # "phila_ccp:260100001" IS a canonical_id even when it is not a registry KEY:
        # match on the entry's own canonical_id first (that is what the id is for)
        key = next((k for k, v in _REG.items()
                    if str((v or {}).get("canonical_id", "")).strip() == str(case_id).strip()
                    and (v or {}).get("caption")), None)
    if key is None:
        docket = ""
        try:
            _cm = _json.loads(
                (_eroot() / "cases_index/case_map.json").read_text())
            docket = str((_cm.get(case_id) or {}).get("case_id", "")).strip()
        except Exception:
            pass
        for k, v in _REG.items():
            if docket and str((v or {}).get("canonical_id", "")).split(":")[-1] == docket:
                key = k
                break
        if key is None:
            # SLUG -> canonical: the registry keys are canonical ids ("phila_ccp:260100000") but
            # every lane passes the obsidian SLUG ("phila_ccp_260100000"). The registry row
            # itself carries `slug`; match on that, then on a normalised id (":"/"_" folded),
            # before the last-resort substring guess. A Superior Court application rendered
            # "PLAINTIFF NAME" on 2026-09-28 because this bridge was missing.
            _norm = lambda x: str(x or "").strip().lower().replace(":", "_").replace("-", "_")
            key = next((k for k, v in _REG.items() if str((v or {}).get("slug", "")).strip() == str(case_id).strip()), None)
            if key is None:
                key = next((k for k, v in _REG.items()
                            if _norm((v or {}).get("canonical_id")) == _norm(case_id) or _norm(k) == _norm(case_id)), None)
        if key is None:
            key = next((k for k in _REG if case_id in k or k in case_id), case_id)
    cap = caption_for(key) or {}
    if not cap.get("plaintiff") or cap["plaintiff"] == "Plaintiff":
        return None
    defs = cap.get("defendants") or ""
    parts = [d.strip() for d in re.split(r"\s+(?:and|&)\s+|,\s*", defs) if d.strip()]
    docket_no = cap.get("case_no") or ""
    # FULL party names when the registry carries them (`caption_parties`, taken from the
    # filed papers): the clerk's abbreviated caption ("FILER VS LIPPI ETAL") is a docket
    # label, not a pleading caption. DATA, per case; the split above stays the fallback.
    _row = (_REG.get(key) or {}) if isinstance(_REG, dict) else {}
    _cp = _row.get("caption_parties") or {}
    plaintiff = _cp.get("plaintiff") or cap["plaintiff"]
    if _cp.get("defendants"):
        parts = [str(d).strip() for d in _cp["defendants"] if str(d).strip()]
    out = {"caption": {"plaintiff": plaintiff,
                       "defendants": [{"name": d, "status": "active"} for d in parts]},
           "docket_no": docket_no,
           "case_no": docket_no,
           "court": {"docket_no": docket_no, "name": cap.get("court") or ""},
           "court_name": cap.get("court") or ""}
    # appellate docket rides along so an appellate-court render can print "No. 0000 EDA 2026"
    if _row.get("appellate_docket"):
        out["appellate_docket"] = str(_row["appellate_docket"])
        out["court"]["appellate_docket"] = str(_row["appellate_docket"])
    return out


def endorsement_block(case_id: str, court: str) -> str:
    """Phila.Civ.R. *210 endorsement. Returns "" where the rule does not apply.

    The rule: briefs "shall be ... endorsed with the name of the case, the court term and
    number, and the name, address, and electronic mail address of the attorney or the party
    if not represented by an attorney."  The filer is not represented, so the PARTY's own
    name/address/email carry the endorsement.

    Driven by the style profile's `endorsement` key (rule-derived, per court) rather than a
    hardcoded court test, so a court whose profile carries no endorsement gets none.
    Fails closed: any missing element -> no block, never a half-endorsement that looks
    compliant.
    """
    spec = _style_profile(court).get("endorsement") or {}
    if not spec.get("requires"):
        return ""
    ctx = registry_caption_ctx(case_id) or {}
    cap = ctx.get("caption") or {}
    plaintiff = (cap.get("plaintiff") or "").strip()
    defs = [d.get("name", "").strip() for d in (cap.get("defendants") or []) if d.get("name")]
    docket = str(ctx.get("docket_no") or "").strip()
    if not plaintiff or not defs or not docket:
        return ""
    name, addr_lines = _sig_address()
    # _sig_address appends the *210 electronic mail address last when configured.
    email = next((l for l in addr_lines if "@" in l), "")
    street = [l for l in addr_lines if "@" not in l]
    if not (name and street and email):
        return ""
    # "et al." is a CONVENTION, not a party. The registry caption carries it as a SUFFIX
    # inside the defendant string ("Example Insurer Et Al"), not as
    # a separate element, so it must be stripped from the name itself -- endorsing a party
    # named "Et Al" misnames the case. Strip, then re-add the abbreviation when the caption
    # said so or real additional defendants remain.
    _etal = False
    _real = []
    for d in defs:
        d2 = re.sub(r"(?i)[,\s]+et\.?\s*al\.?\s*$", "", d.strip()).strip()
        if not d2:
            _etal = True          # the element WAS the et-al token
            continue
        if d2 != d.strip():
            _etal = True          # the element carried an et-al suffix
        _real.append(d2)
    if not _real:
        return ""
    case_name = f"{plaintiff} v. {_real[0]}" + (", et al." if (_etal or len(_real) > 1) else "")
    term = _phila_term(docket)
    term_and_no = f"{term}, No. {docket}" if term else f"No. {docket}"
    return "\n".join([
        "**" + case_name + "**  ",
        term_and_no + "  ",
        name + ", sui juris  ",
        "  \n".join(street) + "  ",
        email,
        "",
        "---",
        "",
    ])


def _phila_term(docket: str) -> str:
    """Philadelphia docket numbers lead with the TERM as YYMM (260100001 -> Sept. Term 2026).

    Rigid identifier, so it is parsed rather than looked up -- but only when the prefix is a
    real year+month; anything else yields "" and the endorsement prints the number alone
    rather than inventing a term.
    """
    m = re.match(r"^(\d{2})(\d{2})\d+$", docket.strip())
    if not m:
        return ""
    yy, mm = int(m.group(1)), int(m.group(2))
    if not 1 <= mm <= 12:
        return ""
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    return f"{months[mm - 1]} Term, 20{yy:02d}"


class CiteGateError(RuntimeError):
    """m1 refused to emit a draft carrying a verifiably wrong citation."""

    def __init__(self, gate):
        self.gate = gate
        lines = [f"  {w['case']} -- pleaded {w['cite']}, corpus has {w['found_cite']}"
                 f"  [{w.get('source','')}]" for w in gate.get("wrong", [])]
        super().__init__(
            "CITE GATE: refusing to emit draft with %d wrong citation(s):\n%s\n"
            "Fix the cite (or correct the corpus) and re-run. Override only with "
            "M1_CITE_GATE=warn, which emits anyway and records the defect in the "
            "sidecar report." % (len(gate.get("wrong", [])), "\n".join(lines)))


def _gate_cites(text, out_path):
    """Verify every case citation in a draft against the corpus before it is written.

    Returns the scan dict (``total``/``verified``/``wrong``/``gaps``); on WRONG_CITE the
    caller raises unless ``M1_CITE_GATE=warn``. A sidecar ``.cites.json`` is written next
    to the draft either way so the verdict is auditable after the fact.

    Failure to LOAD the verifier is never silently fatal to drafting -- that would make a
    tooling problem look like a legal defect -- but it is reported loudly, because a gate
    that quietly no-ops is the condition this function was written to end.
    """
    mode = os.environ.get("M1_CITE_GATE", "block").strip().lower()
    if mode in ("off", "0", "none"):
        print("  [cite-gate] DISABLED via M1_CITE_GATE=off -- draft NOT cite-checked")
        return {}
    try:
        from pipeline.cite_verifier import scan_text
    except Exception as e:
        print(f"  [cite-gate] WARNING: verifier unavailable ({e}) -- draft NOT cite-checked")
        return {}
    try:
        res = scan_text(text)
    except Exception as e:
        print(f"  [cite-gate] WARNING: scan failed ({e}) -- draft NOT cite-checked")
        return {}

    n_ok, n_bad, n_gap = len(res.get("verified", [])), len(res.get("wrong", [])), \
        len(res.get("gaps", []))
    print(f"  [cite-gate] {res.get('total', 0)} cites: {n_ok} verified, "
          f"{n_bad} WRONG, {n_gap} not-in-corpus")
    for w in res.get("wrong", []):
        print(f"    WRONG: {w['case']} {w['cite']} -> corpus has {w['found_cite']}")
    # Gaps warn but never block: a genuine first-impression authority, or one the corpus
    # has not yet acquired, must not be censored out of a filing by its own toolchain.
    for g in res.get("gaps", []):
        print(f"    gap:   {g['case']} {g['cite']} (not in corpus -- verify before filing)")

    try:
        Path(str(Path(out_path).with_suffix("")) + ".cites.json").write_text(
            json.dumps(res, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    if res.get("wrong") and mode == "warn":
        print("  [cite-gate] M1_CITE_GATE=warn -- emitting DESPITE wrong cites")
        return {k: v for k, v in res.items() if k != "wrong"}
    return res


def _write_current_pointer(out_md):
    """Record which draft in a case's drafts/ dir is the operative one.

    Versioned filenames accumulate without a canonical marker (one case reached 87
    `COMPLAINT_M1_FINAL_v*.md` files), so every downstream consumer -- cite gate, exhibit
    builder, mailing packet, a human -- has to guess which is operative, and "newest
    mtime" is wrong the moment a file is copied or an older version is touched. m1 knows
    which draft it just produced, so m1 states it.

    A real symlink is unreliable on /mnt/c (DrvFs), so the plain-text pointer is the
    contract and the symlink is best-effort convenience only.
    """
    try:
        p = Path(out_md)
        d = p.parent
        (d / "CURRENT.txt").write_text(
            f"{p.name}\n# operative draft, written by m1_finisher "
            f"{datetime.datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8")
        link = d / "CURRENT.md"
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(p.name)
        except Exception:
            pass          # DrvFs without symlink support -- CURRENT.txt is the contract
    except Exception as e:
        print(f"  [current-pointer] skipped (non-fatal): {e}")


def resolve_current(drafts_dir):
    """Return the operative draft named by CURRENT.txt, or None when unset/missing."""
    f = Path(drafts_dir) / "CURRENT.txt"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cand = Path(drafts_dir) / line
            return cand if cand.exists() else None
    return None


def _mandatory_sections_for(court, filing_type):
    """Filing-type section contract from the gate's style profile (DATA), [] when none declared."""
    try:
        from pipeline import m1_filing_gate as _g
        return _g._mandatory_sections(court, filing_type) or []
    except Exception:
        return []


def prayer_heading(court, filing_type) -> str:
    """The forum's OWN heading for the prayer (S-finisher-duplicate-organs, 2026-10-25). The
    filing-type contract wins when it mandates one ('RELIEF SOUGHT', Pa.R.C.P. 208.2(a)(3) for the
    PA_CP_PHILA post-order vehicles); else the court's brief contract ('RELIEF', Phila.Civ.R.
    *210(5)); else the federal convention 'PRAYER FOR RELIEF'. Mirrors the gate's own lookup order
    (own kind first, then brief_or_opposition) so the finisher emits the heading the gate keys on."""
    heads = {str(h.get("heading") or "").strip().upper()
             for h in (_mandatory_sections_for(court, filing_type) or [])}
    if "RELIEF SOUGHT" in heads:
        return "RELIEF SOUGHT"
    if "RELIEF" in heads or "RELIEF" in [h.upper() for h, _r, _q in _mandatory_heads(court)]:
        return "RELIEF"
    return "PRAYER FOR RELIEF"


def lane_organs_supplied(court, filing_type) -> dict:
    """Which of the APPLICATION lane's own organs THIS finisher supplies for (court, filing_type):
    {'RELIEF SOUGHT': bool, 'VERIFIED STATEMENT': bool, 'PROOF OF SERVICE': bool}.

    SINGLE SOURCE for both ends of the hand-off: pipeline.m1_application.assemble() asks it and
    does NOT emit an organ the finisher supplies (fix at the source), and finish() strips any
    such organ that still arrives in a body (safety net for bodies built before the fix). The
    finisher is authoritative for the forum's mandatory RELIEF / VERIFICATION / CERTIFICATE OF
    SERVICE headings; a lane whose forum gets no finisher organ (admin paper, affidavit, federal
    verification) keeps its own."""
    ft = (filing_type or "")
    is_admin = ft in ADMIN_TYPES
    is_aff = ft in AFFIDAVIT_FILING_TYPES
    is_screen = _posture_of(ft) == "screen"
    return {
        "RELIEF SOUGHT": not is_admin and not is_aff and not is_screen,
        "VERIFIED STATEMENT": not is_admin and not is_aff and bool(verification_block(court=court)),
        "PROOF OF SERVICE": True,   # certificate_of_service() is appended for EVERY filing type
    }


def forum_posture(court) -> str:
    """'trial' | 'appellate' | '' — read off the renderer's COURT_RULES `is_appellate` flag
    (generation.md_to_docx via m1_filing_gate._court_rules: PA_CP_PHILA/EDPA trial; 3D_CIR/PA_SUPER/
    PA_SUPREME/SCOTUS appellate). The one existing posture map; no table here. '' when the court has
    no rules row (e.g. PA_CMWLTH) -- callers must leave the text alone on ''."""
    try:
        from pipeline.m1_filing_gate import _court_rules
        r = _court_rules(court) or {}
    except Exception:
        r = {}
    if not r or r.get("is_appellate") is None:
        return ""
    return "appellate" if r.get("is_appellate") else "trial"


_REMAND_CLAUSE_RX = re.compile(
    r"\bremand(?:ed|ing)?\b\s*(.*?)(?=\s+(?:to|for|so that|with)\s+|\s*$)", re.I | re.S)


def forum_relief_line(line: str, court: str) -> str:
    """S-relief-forum-vocab (2026-10-25, user-corrected): 'remand' is APPELLATE vocabulary
    (3D_CIR / PA_SUPER / PA_CMWLTH / PA_SUPREME: remand / vacate / reverse) and WRONG in a TRIAL
    forum (PA_CP_PHILA, EDPA): a trial court has nothing to remand TO; its relief is vacate /
    reinstate / direct an answer within twenty (20) days / leave to amend. In a trial forum the
    remand clause is rewritten to 'Reinstate <object> and direct Defendant to answer within
    twenty (20) days' with a logged WARN; in an appellate or unknown forum the line passes
    unchanged. Trailing rule cite preserved."""
    if not line or not re.search(r"\bremand", line, re.I) or forum_posture(court) != "trial":
        return line
    s = line.strip().rstrip(";")
    cite = ""
    m = _TRAILING_CITE_RX.search(s)
    if m:
        cite = m.group(1).strip()
        s = s[:m.start()].rstrip(".").strip()
    s = s.rstrip(".").strip()
    mm = _REMAND_CLAUSE_RX.search(s)
    obj = (mm.group(1) if mm else "").strip().rstrip(",.")
    obj = re.sub(r"^(?:the\s+)?(?:matter|case|action)$", "", obj, flags=re.I).strip()
    fallback = not obj
    if fallback:
        obj = "the dismissed claims"
    clause = f"reinstate {obj} and direct Defendant to answer within twenty (20) days"
    new = s[:mm.start()] + clause if mm else clause
    new = new.strip()
    new = new[0].upper() + new[1:]
    if cite:
        new = f"{new}. {cite}"
    print(f"  [relief-vocab] WARN trial forum {court}: 'remand' is appellate vocabulary -- "
          f"rewrote {line.strip()!r} -> {new!r}" + (" (object not found in the item; generic object used)" if fallback else ""))
    return new


def _strip_lane_organs(body: str, headings) -> tuple:
    """Remove each '## <heading>' section (through the next '#'-heading or EOF) from body.
    Returns (body, [headings actually removed]). Exact heading text, case-insensitive, level-2 only."""
    dropped = []
    for h in headings or []:
        rx = re.compile(rf"(?msi)^##\s+{re.escape(h)}\s*$\n.*?(?=^#{{1,6}}\s|\Z)")
        new, n = rx.subn("", body)
        if n:
            body = new
            dropped.append(h)
    return re.sub(r"\n{3,}", "\n\n", body), dropped


def finish(body_md_path, case_id, court, filing_type, title, out_path,
           relief=None, service_method=None, instrument_hint=None, stub_path=None):
    body = Path(body_md_path).read_text(errors="replace")
    body = re.sub(r"<!--.*?-->\s*", "", body, flags=re.S)  # strip m1 work-product comments
    body = re.sub(r"^# REBUTTAL MATRIX.*$", "", body, flags=re.M).strip()
    # CLASS GUARD A (2026-10-24): internal provenance tags ([corpus:<chunk>], [doc], [official:…], bare chunk ids)
    # are legitimate upstream (relay-room proposal form, m2 cells, OUR_ISSUES) and NEVER reach a court. Strip here,
    # at finish time, and keep every tag in a sidecar beside the output so the chain of custody is not lost
    # (v34 II.1 shipped '28 U.S.C. § 1446(b)(2)(A) [corpus:usc_t28_s1446_c0].' to the Third Circuit screen).
    body, _prov = strip_provenance_tags(body)
    _prov_path = Path(out_path).with_suffix("").with_name(Path(out_path).with_suffix("").name + ".provenance.json")
    if _prov:
        try:
            _prov_path.write_text(json.dumps({"source": str(body_md_path), "tags": _prov}, indent=1, ensure_ascii=False),
                                  encoding="utf-8")
            print(f"  [provenance] {len(_prov)} internal tag(s) stripped from the body -> {_prov_path.name}")
        except Exception as _pe:
            print(f"  [provenance] sidecar not written ({_pe}); {len(_prov)} tag(s) stripped")
    is_admin = filing_type in ADMIN_TYPES
    # An affidavit swears facts. It carries a jurat, not a prayer for relief, and no
    # defects table (that is advocacy, and advocacy cannot appear over a sworn signature).
    is_affidavit = filing_type in AFFIDAVIT_FILING_TYPES
    # S-finisher-duplicate-organs (2026-10-25): the application lane used to write its own RELIEF SOUGHT /
    # VERIFIED STATEMENT / PROOF OF SERVICE while this finisher appended the forum's mandatory RELIEF /
    # VERIFICATION / CERTIFICATE OF SERVICE (the Phila.Civ.R. *210 / Pa.R.C.P. 208.2 headings the gate keys
    # on), so the draft carried both sets (vacate_m2room pass 5 shipped two prayers, two oaths, two service
    # proofs). FIXED AT THE SOURCE: m1_application.assemble() asks lane_organs_supplied() and no longer emits
    # the organs the finisher supplies. The strip below is the safety net for a body built before that fix;
    # lane_organs_supplied() is the single source for both ends, so the two can never disagree.
    _supplied = lane_organs_supplied(court, filing_type)
    body, _dropped = _strip_lane_organs(body, [h for h, w in _supplied.items() if w])
    if _dropped:
        print(f"  [organs] lane duplicate(s) dropped, finisher supplies the forum organ: {', '.join(_dropped)}")
    # S-relief-forum-vocab (2026-10-25): relief vocabulary is keyed by FORUM posture — one pass here so the
    # prayer and the [PROPOSED] ORDER agree ('remand' -> reinstate + answer in 20 days in a trial forum).
    if relief:
        relief = [forum_relief_line(r, court) for r in relief]
    # The body may already open with its own title (m0's organs emit one). Adding the
    # caller's title on top prints it twice. Drop the wrapper title when the body's own
    # first heading already says the same thing.
    # Rule: if the body already opens with its OWN level-1 heading, that heading IS the
    # document's title — adding the caller's on top prints two titles. A body that opens
    # at level 2+ ("## ARGUMENT") is a section, not a title, so the wrapper still applies.
    _first_line = next((l.strip() for l in body.splitlines() if l.strip()), "")
    _dupe_title = _first_line.startswith("# ")
    # A stem-derived title ('07 Brief Opposition Prelim Objections') is a filename, not a filing
    # name. Derive the instrument's real title from what it opposes; keep the caller's only when
    # it is not the stem. Applies to the body's own H1 too when that H1 is the same stem.
    _derived = instrument_title(case_id, instrument_hint or filing_type, court)
    _stemmy = lambda t: bool(re.match(r"^\s*(?:\d+\s+)?[A-Za-z ]+$", t or "")) and \
        re.sub(r"[^a-z]", "", (t or "").lower()) == re.sub(r"[^a-z]", "", (instrument_hint or filing_type or "").lower())
    if _derived and _stemmy(title):
        title = _derived
    if _dupe_title and _derived and _stemmy(_first_line.lstrip("# ").strip()):
        body = body.replace(_first_line, f"# {title}", 1)
    md = [] if _dupe_title else [f"# {title}", ""]
    # Phila.Civ.R. *210 requires the brief itself to be ENDORSED with the case name, the
    # court term and number, and the filer's name/address/email. It is furniture of the
    # brief, so it rides at the head, above the body. Administrative papers and sworn
    # affidavits are not "briefs or memoranda of law" and carry none.
    if not is_affidavit and not is_admin:
        _endorse = endorsement_block(case_id, court)
        if _endorse:
            md.append(_endorse)
    # v2's anatomy (INTRODUCTION / STATEMENT OF THE CASE / STANDARD OF REVIEW),
    # transcribed from m0's stub rather than drafted by an LLM per section.
    if not is_affidavit and not is_admin:
        _anat = anatomy_md(case_id, instrument_hint or filing_type, court)
        if _anat:
            # S-finisher-source-tags (2026-10-25): CASE_FACTS provenance tags are not filing text
            _anat = re.sub(r"\s*\[\s*(?:Source|corpus|doc|official|record)\s*:[^\]\n]*\]", "", _anat)
            md.append(_anat)
            md.append("## ARGUMENT")
    md.append(body)
    # Administrative instruments take NO defects ledger, for the same reason they take no
    # anatomy and no prayer: the table is drawn from the case-wide audit stream, so on a
    # Pa.R.A.P. 123 application to compel record transmission it appended FRCP 60(d)(3),
    # 5 U.S.C. § 706 and Pa.R.C.P. 1024 defects -- federal merits material with no bearing
    # on whether a state appellate prothonotary must issue a 1935(a) notice. Irrelevant
    # authority in an administrative paper is not padding; it is a target.
    _is_screen = _posture_of(filing_type) == "screen"
    if not is_affidavit and not is_admin and not _is_screen:
        md.append(defects_table(case_id))
    # 6. PRAYER FOR RELIEF -- an opposition that states no request leaves the court
    # nothing to enter. Sworn affidavits and administrative papers carry none.
    if _is_screen:
        # m1_screen owns the screening relief (L.A.R. 27.4(b) referral), stated by the APPELLANT. No reservation
        # block (nothing is opposed) and no proposed order (a court of appeals enters its own).
        from pipeline import m1_screen as _scr
        md.append(_scr.relief_block(_sig_address()[0]))
    elif not is_affidavit and not is_admin:
        _pray = prayer_for_relief(case_id, instrument_hint or filing_type, relief, court)
        # Phila.Civ.R. *210(5) names the section RELIEF; Pa.R.C.P. 208.2(a)(3) vehicles carry RELIEF SOUGHT;
        # the federal convention is PRAYER FOR RELIEF. Rename to the forum's own mandatory heading so the
        # filing satisfies the rule's "separately and distinctly titled" requirement (prayer_heading mirrors
        # the gate's own contract lookup order).
        _ph = prayer_heading(court, filing_type)
        if _pray and _ph != "PRAYER FOR RELIEF":
            _pray = re.sub(r"(?m)^##\s*PRAYER FOR RELIEF\s*$", f"## {_ph}", _pray)
        md.append(_pray)
        # RESERVATION OF RIGHTS -- every opposition, every forum. Without it an adverse
        # reading treats anything unaddressed as conceded; with it the movant's silences
        # are the waivers (the locked symmetry). Never a leave-to-amend request.
        if _posture_of(filing_type) == "opposition":
            md.append(reservation_block(instrument_hint or filing_type, court))
    # The court MUST reach the signature block: signature_block() chooses the
    # appearance designation from it, and calling it bare made every PA state
    # filing recite "Appearing personally under 28 U.S.C. § 1654" — the FEDERAL
    # appearance statute, wrong on its face in the Court of Common Pleas.
    # RULE-MANDATED ATTACHMENTS (GROUND contract -> DRAFT). Exhibits ride inside the
    # document as an index and are copied into the packet dir as separately marked files.
    _att_plan = []
    if not is_affidavit and not is_admin:
        try:
            _att_md, _att_plan = attachments_md(case_id, court, filing_type, instrument_hint or "")
            if _att_md:
                md.append(_att_md)
        except Exception as _ae:
            print(f"  [attachments] skipped (non-fatal): {_ae}")
    md.append(signature_block(court=court))
    # Pa.R.C.P. 1024 VERIFICATION -- a pleading that answers factual averments must be
    # verified, and an unverified pleading is strikeable. A sworn affidavit already
    # carries its own oath, and an administrative paper is not a pleading, so neither
    # takes one. verification_block() itself returns "" outside PA state courts, since
    # the federal analogue (28 U.S.C. 1746) is worded differently.
    if not is_affidavit and not is_admin:
        md.append(verification_block(court=court))
    md.append(certificate_of_service(service_method or "first-class United States Mail, postage prepaid",
                                     recipients=(service_recipients(stub_path) if stub_path else None)))
    # an opposition always carries a prayer: without one the filing states no
    # relief for the court to enter.
    if not is_affidavit and not _is_screen and (relief is not None
                             or (_posture_of(filing_type) == "opposition" and not is_admin)):
        # the lane normalizes filing_type to a bare posture word ("opposition"), which
        # no longer names WHAT is opposed — the caller passes the stub's own name so the
        # order can say "Preliminary Objections"/"Motion to Dismiss" instead of "Motion".
        md.append(proposed_order(relief, filing_type=(instrument_hint or filing_type),
                                 posture_type=filing_type, court=court))
    full = "\n\n".join(x for x in md if x)
    # m0's OWN definitions organs (Schedule A + quantum superscript footnotes)
    if os.environ.get("M1_SCREEN_PAGE_LIMIT"):
        print(f"  [definitions] Schedule A omitted: court page limit {os.environ['M1_SCREEN_PAGE_LIMIT']} pages")
    elif not is_admin:
        try:
            from pipeline import definitions_tagger
            definitions_tagger.set_forum(court)   # forum by identifier, never by body scan
            full = definitions_tagger.inject_into_draft(full, case_id)
            full = definitions_tagger.inject_quantum(full, case_id, ground=False, sup="unicode")
        except Exception as e:
            print(f"  [definitions] skipped (non-fatal): {e}")
    # CITE GATE (hard stop). A verifier nothing calls is not a gate: cite_verifier.py sat
    # in the tree for months marked out-of-flow while m1 emitted drafts unchecked, and the
    # contaminated Rancosky cite reached a draft precisely because no emit path consulted it.
    # WRONG_CITE = refuse to write. A wrong reporter cite in a filed document is worse than
    # no document: it is the kind of defect that gets a pro se litigant's brief disregarded
    # wholesale, which is the exact failure this system exists to prevent.
    _gate = _gate_cites(full, out_path)
    if _gate.get("wrong"):
        raise CiteGateError(_gate)

    out_md = Path(out_path).with_suffix(".md")
    out_md.write_text(full, encoding="utf-8")
    _write_current_pointer(out_md)
    # packet files: <same index prefix as this doc>_<EXHIBIT_x_...> so the gate's
    # same-index sibling rule sees them as THIS filing's attachments
    if _att_plan:
        import shutil as _sh
        _prefix = out_md.name.split("_", 1)[0] + "_" if "_" in out_md.name else ""
        for _name, _src in _att_plan:
            _dst = out_md.parent / f"{_prefix}{_name}"
            try:
                if _src is None:
                    _dst.write_text("REQUIRED BY RULE -- supply before filing.\n", encoding="utf-8")
                    print(f"  [attachments] TODO {_dst.name}")
                else:
                    _sh.copyfile(_src, _dst)
                    print(f"  [attachments] {_dst.name} <- {Path(_src).name}")
            except Exception as _ce:
                print(f"  [attachments] copy failed {_dst.name}: {_ce}")
    # m0's OWN renderer (caption, fonts, spacing, numbering per COURT_RULES)
    # contract: render_md_to_docx(md_text, out_path, court_id=..., ctx=...) -> path
    from generation.md_to_docx import render_md_to_docx
    docx_path = str(Path(out_path).with_suffix(".docx"))
    # CAPTION_HYDRATE_V1: the renderer falls back to "PLAINTIFF NAME" / "Case No. ____"
    # when ctx carries no caption. pipeline.auto_reply.caption_for() already resolves the
    # registry entry (court, parties, docket) for any case_id — reuse that organ rather
    # than letting the placeholder reach a filed document.
    ctx = {"case_id": case_id}
    _reg = registry_caption_ctx(case_id)
    if _reg:
        ctx.update(_reg)
        print(f"  [caption] registry: {_reg['caption']['plaintiff']} "
              f"(No. {_reg.get('docket_no','')})")
    else:
        print(f"  [caption] WARNING: no registry caption for '{case_id}' — "
              f"docx will carry placeholder names")
    # APPELLATE CAPTION: one district case can carry several appeals; the appeal no. + parties are m0's (stub ## CAPTION,
    # from the Clerk's own order), never the registry's single field. Without this the docx printed 'No. ________'.
    if stub_path:
        try:
            from pipeline import m1_screen as _s
            _cl, _apd = _s.caption_of(Path(stub_path).read_text(encoding="utf-8", errors="replace"))
            if _apd:
                ctx["appellate_docket"] = _apd
                if isinstance(ctx.get("court"), dict):
                    ctx["court"]["appellate_docket"] = _apd
                _pm = re.search(r"No\.\s*\d{2}-\d{3,5}\s*—\s*(.+?)\s+v\.\s+(.+?)\s*(?:\(|$)", _cl)
                if _pm:
                    _cap = dict(ctx.get("caption") or {})
                    _cap["plaintiff"] = _pm.group(1).strip()
                    _cap["defendants"] = [{"name": _pm.group(2).strip().rstrip(",")}]
                    ctx["caption"] = _cap
                print(f"  [caption] appellate No. {_apd} from stub")
        except Exception as _e:
            print(f"  [caption] appellate caption skipped (non-fatal): {_e}")
    _ctl = None if ctx.get("appellate_docket") else adverse_control_no(case_id, instrument_hint or filing_type)
    if _ctl:
        ctx["control_no"] = _ctl
        print(f"  [caption] control no: {_ctl} (from CASE_TIMELINE, opposed instrument)")
    else:
        print(f"  [caption] control no: none found in CASE_TIMELINE for '{instrument_hint or filing_type}'")
    docx = render_md_to_docx(full, docx_path, court_id=court, ctx=ctx)
    print(f"finished: {out_md.name} -> {Path(docx).name if docx else 'render failed'}")
    return str(out_md), docx

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("body"); ap.add_argument("--case", required=True)
    ap.add_argument("--court", required=True); ap.add_argument("--type", required=True, dest="ftype")
    ap.add_argument("--title", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    finish(a.body, a.case, a.court, a.ftype, a.title, a.out)

if __name__ == "__main__":
    main()
