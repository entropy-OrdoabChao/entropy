"""tactics_detector — the catalog of recurring avoidance TRICKS that dispose of pro se cases without
reaching the merits, each with the controlling COUNTER. Three modes:

  mode="draft"    — scan OUR draft for the pleading defects that INVITE these attacks; fix before filing.
  mode="opposing" — scan an opposing brief/answer for the tricks; emit the counter (cite + move).
  mode="court"    — scan a court order/docket for judicial avoidance; emit the counter + the judicial-
                    complaint fodder (28 U.S.C. 351 / 455 / 144, mandamus 1651, Rule 60).

The point (the user's lived problem): preempt the "failure to state a claim" / entity-name-dodge /
concession-walkback / sitting-on-motions moves so the merits must be reached and the judicial complaints
land. Conventional doctrine + controlling cites — corpus-verifiable, never invented.

Non-destructive (reads text; writes output/tactics/). Deterministic. Cites should be verified against
the corpus (the SCOTUS U.S. Reports volumes + FRCP/FRE are ingested).

CLI:
  python -m pipeline.tactics_detector --file ourdraft.md --mode draft
  python -m pipeline.tactics_detector --file opp_motion.txt --mode opposing
  python -m pipeline.tactics_detector --file order.txt --mode court
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
OUT = ROOT / "output" / "tactics"

# Each tactic: name | mode | pattern (regex, case-insensitive) | what it is | the COUNTER (cite + move).
_TACTICS = [
    # ---- OUR-DRAFT defects to PREEMPT (fix before filing so the attack has no foothold) ----
    {"name": "shotgun pleading", "mode": "draft", "pattern": r"reincorporat\w+|re-?alleg\w+ (?:each|all|the foregoing|paragraphs? 1)",
     "is": "a count that incorporates every prior paragraph -> court cannot map facts to elements",
     "counter": "Restrict each count to the specific paragraphs establishing ITS elements (Weiland v. Palm Beach Cty. Sheriff, 792 F.3d 1313 (11th Cir. 2015))."},
    {"name": "group pleading", "mode": "draft", "pattern": r"\bDefendants\b(?!,? (?:and|or)\b)",
     "is": "refers to defendants collectively where individual conduct matters (Iqbal fair-notice)",
     "counter": "Name the SPECIFIC defendant in every factual paragraph; one actor per sentence (Ashcroft v. Iqbal, 556 U.S. 662)."},
    {"name": "conclusory allegation", "mode": "draft", "pattern": r"\b(?:willfully|negligently|wrongfully|unlawfully) (?:violated|breached|failed)\b",
     "is": "a legal characterization not preceded by the underlying facts (Twombly/Iqbal plausibility)",
     "counter": "Precede each conclusion with the dated, sourced facts that establish each element (Bell Atlantic v. Twombly, 550 U.S. 544)."},
    {"name": "missing standing paragraph", "mode": "draft", "pattern": r"^(?!.*injury[- ]in[- ]fact).*$", "whole": "standing",
     "is": "no explicit injury-in-fact / traceability / redressability allegation",
     "counter": "Add a standing paragraph: concrete injury, traceable to defendant, redressable (Spokeo 578 U.S. 330; TransUnion 594 U.S. 413)."},
    {"name": "missing jurisdiction/venue statement", "mode": "draft", "pattern": r"", "whole": "juris_venue",
     "is": "no specific jurisdiction (28 U.S.C. 1331/1332) or venue (28 U.S.C. 1391) paragraph",
     "counter": "Plead the jurisdiction basis with facts + a venue paragraph (FRCP 8(a)(1); 28 U.S.C. 1391)."},
    {"name": "statute-of-limitations not pre-pled", "mode": "draft", "pattern": r"", "whole": "sol",
     "is": "facial SOL risk with no discovery-rule / tolling / continuing-violation allegation",
     "counter": "Affirmatively plead the discovery rule / tolling / continuing violation with facts (Cada v. Baxter, 920 F.2d 446)."},
    {"name": "entity not fully identified", "mode": "draft", "pattern": r"", "whole": "entity_id",
     "is": "a defendant named without full legal name + entity type -> invites the name-isnt-exact dodge",
     "counter": "Plead each defendant's FULL legal name, entity type, state of incorporation, and registration; define it (Schedule A) and use it mechanically."},

    # ---- OPPOSING tricks to COUNTER ----
    {"name": "boilerplate discovery objection", "mode": "opposing", "pattern": r"vague,?\s*ambiguous|overbroad|unduly burdensome|not reasonably calculated",
     "is": "canned objections disapproved by the 2015 Rule amendments",
     "counter": "Move to compel: objections need specific reasons (FRCP 34(b)(2)(B)); the test is proportionality, not 'reasonably calculated' (FRCP 26(b)(1), 2015 amendment)."},
    {"name": "see-documents-produced dodge", "mode": "opposing", "pattern": r"see (?:the )?documents? (?:produced|attached)|refer to (?:the )?production",
     "is": "interrogatory answered by pointing at an undifferentiated production",
     "counter": "FRCP 33(d) permits records-reference ONLY if the burden is substantially the same AND the specific records are identified; move to compel specific identification."},
    {"name": "entity-name-dodge", "mode": "opposing", "pattern": r"(?:name is not|misnamed|improperly named|no such entity|not the correct (?:party|entity)|exact name)",
     "is": "defendant claims the named entity is wrong/inexact to avoid the merits",
     "counter": "Defeat with the FULL legal name + judicially-noticeable proof (SEC/EDGAR filing, state registration) under FRE 201(c)(2) (mandatory on request); amend party name under FRCP 15(c)/21 (misnomer relates back); the dodge does not reach the merits."},
    {"name": "concession-then-walkback / ignored record evidence", "mode": "opposing",
     "pattern": r"(?:we offer|markets?|labels?|affiliat\w+|in some sense|generally|loosely)\b.*(?:product|services)|admits? .{0,40}\bbut\b",
     "is": "counsel concedes a fact then vaguely walks it back while ignoring record exhibits (the SEC docs)",
     "counter": "Pin the admission: FRCP 8(b)(6) (un-denied averment admitted); request admissions FRCP 36 + move to deem admitted (36(a)(3)); the conceded/record fact is judicially noticeable (FRE 201) and must be accepted on a 12(b)(6); a vague walk-back does not create a fact dispute."},
    {"name": "selective quotation / overruled cite", "mode": "opposing", "pattern": r"",
     "whole": "cite_check",
     "is": "case quoted out of context or no-longer-good-law relied upon",
     "counter": "Shepardize/KeyCite every cite; in response give the FULL quote + controlling holding and flag overruled/distinguished authority."},
    {"name": "sham affidavit at summary judgment", "mode": "opposing", "pattern": r"declaration|affidavit|sworn statement",
     "is": "a declaration contradicting prior deposition testimony to manufacture a dispute",
     "counter": "Sham-affidavit doctrine: the court disregards a declaration that contradicts prior sworn testimony without explanation (Cleveland v. Policy Mgmt. Sys., 526 U.S. 795); also FRE 602 personal knowledge / FRCP 56(c)(4)."},

    # ---- COURT (judicial) avoidance to COUNTER + feed the judicial complaint ----
    {"name": "sitting on motions / undue delay", "mode": "court", "pattern": r"",
     "whole": "delay",
     "is": "court holds a ripe motion for months without ruling",
     "counter": "Motion to compel ruling; petition for writ of mandamus to the circuit (28 U.S.C. 1651; Cheney v. U.S. Dist. Ct., 542 U.S. 367); document for the 28 U.S.C. 351 complaint ('conduct prejudicial to the effective and expeditious administration')."},
    {"name": "sua sponte ruling without notice", "mode": "court", "pattern": r"sua sponte|on the court'?s own (?:motion|initiative)",
     "is": "court rules without notice/opportunity to respond",
     "counter": "Due process requires notice + opportunity to be heard (Mullane v. Central Hanover, 339 U.S. 306); move for reconsideration on that ground; 28 U.S.C. 351 fodder."},
    {"name": "no written findings / reasoned order", "mode": "court", "pattern": r"denied\.?\s*$|granted\.?\s*$|so ordered",
     "is": "ruling without the findings of fact / conclusions of law required",
     "counter": "FRCP 52(a) requires findings; move for an amended order stating reasons; an unreasoned denial of a meritorious motion is abuse of discretion + appealable."},
    {"name": "failure-to-state-a-claim as avoidance", "mode": "court", "pattern": r"fail(?:ure|s|ed)? to state",
     "is": "12(b)(6) used to dodge well-pleaded, record-supported elements",
     "counter": "On 12(b)(6) the court must accept well-pleaded facts as true (Twombly/Iqbal); pro se gets liberal construction + notice-and-amend (Haines v. Kerner 404 U.S. 519; Alston v. Parker 363 F.3d 229 (3d Cir.)); cite the Schedule-A element-mapped paragraphs + the judicially-noticeable record."},
    {"name": "strike pro se filing as non-compliant", "mode": "court", "pattern": r"stricken|struck|non-?compliant|does not comply",
     "is": "filing struck on a technicality",
     "counter": "File a corrected version immediately; substantial-compliance + liberal leave to amend (Foman v. Davis 371 U.S. 178); pro se liberal construction (Haines)."},
]

# heuristics for the "whole-document" checks (no simple regex)
def _whole_checks(text: str, mode: str):
    t = text.lower(); flags = []
    if mode == "draft":
        if "injury-in-fact" not in t and "injury in fact" not in t and "standing" not in t:
            flags.append("missing standing paragraph")
        if not re.search(r"jurisdiction", t) or not re.search(r"\bvenue\b", t):
            flags.append("missing jurisdiction/venue statement")
        if not re.search(r"discovery rule|tolling|continuing violation|relation back", t):
            flags.append("statute-of-limitations not pre-pled")
        if not re.search(r"(corporation|limited liability|l\.?l\.?c\.?|incorporated|\binc\.?\b|n\.?a\.?\b|state of incorporation|registered)", t):
            flags.append("entity not fully identified")
    return flags


def scan(text: str, mode: str = "draft") -> list[dict]:
    out, low = [], text or ""
    wholeset = set(_whole_checks(low, mode))
    for t in _TACTICS:
        if t["mode"] != mode:
            continue
        if t.get("whole"):
            if t["name"] in wholeset:
                out.append({"tactic": t["name"], "is": t["is"], "counter": t["counter"], "where": "(document-level)"})
            continue
        if not t["pattern"]:
            continue
        m = re.search(t["pattern"], low, re.I | re.M)
        if m:
            s = max(0, m.start() - 50); e = min(len(low), m.end() + 50)
            out.append({"tactic": t["name"], "is": t["is"], "counter": t["counter"],
                        "where": "…" + low[s:e].replace("\n", " ").strip() + "…"})
    return out


def report(text: str, mode: str = "draft", slug: str = "") -> dict:
    flags = scan(text, mode)
    OUT.mkdir(parents=True, exist_ok=True)
    hdr = {"draft": "OUR-DRAFT defects to FIX before filing (preempt the attack)",
           "opposing": "OPPOSING tricks detected -> COUNTER",
           "court": "JUDICIAL avoidance detected -> COUNTER + 28 U.S.C. 351 fodder"}.get(mode, mode)
    md = [f"# Tactics scan ({mode}) — {slug or '(doc)'}", hdr, f"\n{len(flags)} flag(s).", ""]
    for f in flags:
        md += [f"## {f['tactic']}", f"- it is: {f['is']}", f"- where: {f['where']}", f"- **COUNTER:** {f['counter']}", ""]
    (OUT / f"tactics_{mode}_{slug or 'doc'}.json").write_text(json.dumps({"mode": mode, "flags": flags}, indent=2), encoding="utf-8")
    (OUT / f"tactics_{mode}_{slug or 'doc'}.md").write_text("\n".join(md), encoding="utf-8")
    return {"mode": mode, "slug": slug, "flags": flags}


if __name__ == "__main__":
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "draft"
    fp = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
    slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else ""
    text = Path(fp).read_text(encoding="utf-8", errors="ignore") if fp else (
        "Defendants willfully violated FCRA. Defendant State Farm says it offers and labels products. "
        "Counsel admits the relationship but says the company name is not exact. The motion was denied.")
    r = report(text, mode=mode, slug=slug)
    print(f"tactics ({mode}): {len(r['flags'])} flag(s)")
    for f in r["flags"]:
        print(f"  - {f['tactic']}")
