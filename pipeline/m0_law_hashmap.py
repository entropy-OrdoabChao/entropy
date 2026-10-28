#!/usr/bin/env python3
"""m0_law_hashmap — Phase E: the deterministic claim→law index in the vault.

USER DOCTRINE (2026-08-31, verbatim): "this really feels like it is math and a
hashmap that obsidian link already built. every case can be won... only thing
It can't do is control human discretion or choice to not do their job, but as
long as we expose that bias, corruption or desire to not do it we then put it
on the record."

WHAT IT DOES (deterministic ONLY — no LLM; the sources are already
LLM-comprehended + corpus-verified upstream):
  For each claim pattern in m0_authority_spine.SPINE, write ONE vault note
  obsidian_sync/law_hashmap/claim-<slug>.md carrying the full O(1) lookup row:
    CLAIM → ELEMENTS (from the spine plead sentences)
          → CONTROLLING AUTHORITY (name, cite, court, corpus source, verified)
          → PROOF TYPE (which lane: PLEAD/EVIDENCE/REFER/CRIMINAL, from the
            duty maps that share the pattern's vocabulary)
          → TRAP (the known side-step trick for this claim class, from the
            tricks list — encoded so it can't work silently again)
          → DISCRETION-EXPOSURE (the mandatory 'shall' lever + who holds it,
            from the accountability maps)
  Each note gets frontmatter tags + [[wikilinks]] to the cases that plead the
  pattern, so obsidian_link's graph walks CASE → CLAIM → LAW deterministically.

VERIFICATION GATE: every authority row re-verifies through the spine's own
_verbatim() before it is written. A row that fails verification is written as
NOT-VERIFIED with a loud banner (never silently included OR dropped).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VAULT = ROOT / "obsidian_sync"
OUT = VAULT / "law_hashmap"

# the specimen tricks, per claim family (encoded from the cases — user: each
# trick is a specimen so the next suit sees it coming)
TRAPS = {
    "bad faith|8371": "Standard-swap: court applies common-law bad faith instead of the § 8371 statutory two-prong (Rancosky needs NO ill-will showing). Counter: plead Rancosky's prongs verbatim; object to any 'self-interest or ill-will' element as Terletsky-superseded.",
    "utpcpl|201-": "State-of-mind smuggling: defense argues intent/knowledge under the catch-all. Gregg kills it — deceptive conduct is measured by tendency to deceive, strict liability. Counter: quote Gregg; demand the element list on the record.",
    "personal jurisdiction|registration|foreign corp": "The removal/dismissal two-step on jurisdiction. Mallory: PA registration = consent to general jurisdiction. Counter: plead the registration fact + Mallory in the complaint body.",
    "breach of contract": "Gist-of-the-action dismissal of the contract count's tort siblings. Bruno: duties imposed by law sound in tort DESPITE the contract. Counter: plead the social-policy duty separately from the contract terms.",
    "unjust enrichment": "'Contract governs' dismissal of the UE count. Counter: plead UE in the alternative for benefits outside the contract's subject (post-termination premiums are never 'governed' by a canceled policy).",
    "conversion": "Same gist-of-the-action shield as UE; Bruno controls.",
    "fcra|1681|credit report": "The 'parroting' defense: CRA claims it 'verified' by re-asking the furnisher. Cushman: reinvestigation must be REASONABLE — relying on the furnisher's say-so is not. Trap two: 'no actual damages' — Safeco: willful includes reckless disregard → statutory + punitive without actual harm.",
    "respa|2605|qualified written request|qwr|servicer": "Servicer recharacterizes the QWR as 'correspondence' to dodge the § 2605(e) clock. Counter: the letter's content controls, not its label; 1024.35 error-resolution duties attach on ANY written error assertion.",
    "quiet title|chain of title|real party": "Enforcement entity pleads around the chain: servicer sues without the note, assignment gaps papered with late/robo allonges. Counter: Pa.R.C.P. 2002 real-party + 1061 quiet-title; demand the wet-ink note and EVERY assignment in discovery (burden-flip RFPs in cmn_prop_001).",
    "garnish|3145|judgment enforcement": "Garnishee overreaches: answers with the DEFENDANT's defenses or attacks the attachment. 3145(b) forbids both. Counter: move to strike any garnishee defense outside the rule's closed list.",
    "recusal|disqualif|bias|impartial": "The 'no proof of actual bias' dodge. Caperton: the PROBABILITY of bias is the constitutional test; Murchison: appearance of justice suffices. Counter: plead the objective circumstances (relationship, prior knowledge, conflict nexus); demand ruling on the record.",
}

# the mandatory-duty levers (accountability-map yield, corpus-verified)
LEVERS = {
    "bad faith|8371": "PA DOI: 40 P.S. § 323.3(b) — 'shall at a minimum conduct an examination of every insurer... not less frequently than once every five years' [corpus: pa_ps_t40_s323_3]. CFPB: 12 U.S.C. § 5534(a) shall-respond [corpus: usc_t12_s5534]; § 5534(b) forces the company itself to answer on the record.",
    "utpcpl|201-": "PA AG / consumer protection referral lane; UTPCPL private right + treble (Schwartz). DOI lever applies when the actor is an insurer.",
    "fcra|1681|credit report": "CFPB § 5534 complaint = mandatory response; FTC § 45 referral (NOTE: § 45 text NOT IN CORPUS — referral framed on § 5531 UDAAP instead). DOJ: 28 U.S.C. § 547(1) 'shall prosecute' [corpus: usc_t28_s547] for §§ 1341/1343 referrals; declination letter = public-record exhibit.",
    "respa|2605|qualified written request|qwr|servicer": "CFPB § 5534 + Reg X supervisory lane. FNMA guides are CONTRACTUAL not statutory (honest limit, accountability run 2026-08-31) — enforce via the servicing contract's incorporation, not a public 'shall'.",
    "quiet title|chain of title|real party": "Recorder-of-deeds record integrity; UCC Art. 3/9 holder rules; the burden-flip is judicial not administrative — the lever is Rule 2002's OWN mandatory text ('shall be prosecuted by... the real party in interest').",
    "garnish|3145|judgment enforcement": "The rule text itself is the lever — 3145(b)'s closed list is mandatory. Judicial-conduct lane (JCB complaint) when the tribunal lets the garnishee exceed it.",
    "recusal|disqualif|bias|impartial": "28 U.S.C. § 455 / Pa. Code of Judicial Conduct mandatory-recusal circumstances; JCB complaint = the administrative record; Caperton = the constitutional floor no rule can lower.",
}


def _slug(pat: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", pat.lower()).strip("-")[:48]


def _claim_matches(pat, blob, entries=None):
    """Does this case's text implicate the claim pattern `pat`?

    Regex over case prose is a keyword table pretending to comprehend, and it failed both
    ways here: `pat.split("|")` shattered parenthesized groups (a hard crash that aborted
    the whole build), and even intact it under-matched -- conversion linked 3 cases where
    the corrected matcher finds 9.

    The reliable signal is NOT the pattern string, it is the AUTHORITY the spine already
    carries for the claim: when a case file names the controlling authority (party name or
    reporter cite), that case implicates the claim whatever words the note happens to use.
    Pattern matching remains a fallback for rows whose authority is not yet named in the
    case file, but it is now the second-choice signal rather than the only one.
    """
    if entries:
        for en in entries:
            party = (en.get("name") or "").split(" v. ")[0].strip()
            if len(party) > 3 and party.lower() in blob:
                return True
            m = re.match(r"\s*(\d+)\s+([A-Za-z.\s]+?)\s+(\d+)", en.get("cite") or "")
            if m and f"{m.group(1)} {m.group(2).strip()} {m.group(3)}".lower() in blob:
                return True
    try:
        return bool(re.search(pat, blob))
    except re.error as e:
        print(f"  [law_hashmap] WARNING: bad SPINE pattern {pat!r}: {e}")
        return False


def build() -> int:
    from pipeline.m0_authority_spine import SPINE, _con, _verbatim
    con = _con()
    OUT.mkdir(exist_ok=True)
    # which cases plead which pattern (from their exposure maps + IC sections)
    case_links: dict[str, list[str]] = {}
    for cdir in sorted((VAULT / "cases").glob("*/")):
        blob = ""
        for fn in ("EXPOSURE_MAP.md", "CASE_FACTS.md"):
            p = cdir / fn
            if p.exists():
                blob += p.read_text(encoding="utf-8", errors="ignore")[:60000].lower()
        for pat in SPINE:
            if _claim_matches(pat, blob, SPINE.get(pat)):
                case_links.setdefault(pat, []).append(cdir.name.rstrip("/"))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_ok = n_unver = 0
    for pat, entries in SPINE.items():
        slug = _slug(pat)
        lines = [
            "---",
            f"id: claim-{slug}",
            f'title: "LAW_HASHMAP: {pat}"',
            "tags: [law-hashmap, claim-index, m0, stare-decisis]",
            f"updated: {ts}",
            "---",
            "",
            f"# CLAIM PATTERN: `{pat}`",
            "",
            "*O(1) row: claim → elements → authority → proof → trap → discretion-exposure.*",
            "*Deterministic index; every authority re-verified against corpus at build time.*",
            "",
            "## CONTROLLING AUTHORITY (stare decisis)",
            "",
        ]
        for e in entries:
            got = _verbatim(con, e["source"], e["locators"])
            if got:
                n_ok += 1
                mark = f"**VERIFIED** [corpus: {e['source']}]"
            else:
                n_unver += 1
                mark = f"⚠️ **NOT-VERIFIED at build** (source: {e['source']}) — do not weave until re-verified"
            lines += [
                f"### {e['name']}, {e['cite']} ({e['court']})",
                f"- {mark}",
                f"- **Point:** {e['point']}",
                f"- **Ready-to-plead:** {e['plead']}",
                "",
            ]
        lines += ["## TRAP (the known side-step — encoded specimen)", "",
                  TRAPS.get(pat, "No specimen recorded yet for this pattern — add when a case surfaces one."), "",
                  "## DISCRETION-EXPOSURE (the mandatory lever + who holds it)", "",
                  LEVERS.get(pat, "No administrative lever mapped — judicial-lane only (or add from next accountability run)."), "",
                  "## CASES PLEADING THIS PATTERN", ""]
        for c in case_links.get(pat, []):
            lines.append(f"- [[cases/{c}/CASE_FACTS|{c}]] ([[cases/{c}/EXPOSURE_MAP|exposure map]])")
        if not case_links.get(pat):
            lines.append("- none yet (pattern held in reserve)")
        lines += ["", "---", "*Built by pipeline.m0_law_hashmap — regenerate after any SPINE/duty-map/accountability change.*"]
        (OUT / f"claim-{slug}.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"[hashmap] claim-{slug}.md ({len(entries)} authorities, "
              f"{len(case_links.get(pat, []))} case links)")
    # index note
    idx = ["---", "id: law-hashmap-index", 'title: "LAW_HASHMAP index"',
           "tags: [law-hashmap, moc]", f"updated: {ts}", "---", "",
           "# LAW_HASHMAP — claim → law, O(1)", "",
           "The deterministic layer: every claim pattern the engine can plead, "
           "with verified controlling authority, the known trap, and the "
           "mandatory-duty lever. The graph edge CASE → CLAIM → LAW.", ""]
    for pat in SPINE:
        idx.append(f"- [[law_hashmap/claim-{_slug(pat)}|{pat}]]")
    (OUT / "INDEX.md").write_text("\n".join(idx), encoding="utf-8")
    print(f"[hashmap] INDEX.md | verified {n_ok}, not-verified {n_unver}")
    return 0 if n_unver == 0 else 2


if __name__ == "__main__":
    raise SystemExit(build())
