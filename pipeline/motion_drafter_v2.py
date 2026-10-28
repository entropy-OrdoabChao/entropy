from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from pipeline.drafter_grounding import (get_section_grounding, get_canon_grounding,
                                        get_restoration_grounding, get_pseudolaw_rebuttal_grounding)
#!/usr/bin/env python3
"""
Bundle 12 Step 2 - motion_drafter_v2
Expands a stub motion .md (produced by render_packet) into a full draft
using the local llama-server at http://localhost:8000.

GROUND-TRUTH ONLY: prompt forbids invented citations, invented facts, and
pseudolaw arguments. The LLM is given the stub's structured data
(controlling_authority, opposing_authority, scanner_corroboration) plus
optional dictionary lookups, and must cite only what's in the input.

Usage:
  python3 motion_drafter_v2.py <path/to/stub.md>
  python3 motion_drafter_v2.py --case fed:2:26-cv-00123 --priority 10
  python3 motion_drafter_v2.py --case fed:2:26-cv-00123 --chain 1
  python3 motion_drafter_v2.py --batch <packet_dir>  # all stubs in packet
  python3 motion_drafter_v2.py --dry-run <stub>      # print prompt only

Output: writes <stub_basename>_DRAFT.md next to the stub.
"""
import sys
import os
import re
import json
import argparse
import time
from pathlib import Path
from datetime import datetime

import requests

REPO = _eroot()
LLAMA_URL = os.environ.get('LLAMA_URL', 'http://localhost:8000')
PLAINTIFF_NAME = os.environ.get('PLAINTIFF_NAME', 'Filer Filer')


SYSTEM_PROMPT = (
    "You are a careful federal pro se legal drafting assistant. You produce "
    "argument sections for motions filed by pro se plaintiff "
    f"{PLAINTIFF_NAME}.\n\n"
    "STRICT RULES:\n"
    "1. Use ONLY the facts and authorities provided in the user input. "
    "Do not invent citations, parties, statutes, dates, dollar amounts, "
    "ECF numbers, or events.\n"
    "2. Every legal proposition must cite an authority that appears in the "
    "input (controlling_authority, opposing_authority, or scanner_corroboration).\n"
    "3. Every factual assertion must reference a scanner-corroborated quote "
    "by source_doc, or quote directly from a cited authority.\n"
    "4. The corpus and the Obsidian graph decide what is and is not law -- "
    "there is NO banned-topic list. If opposing counsel labels the plaintiff's "
    "position 'frivolous,' 'pseudolaw,' 'sovereign citizen,' 'redemptionist,' "
    "or the like, REBUT it with full corpus backing: name the doctrine and cite "
    "the statute/UCC/CFR/IRS/Black's authority (provided in the grounding "
    "below) that establishes it IS settled law -- e.g. UCC Article 3/9 "
    "right-to-enforce & holder-in-due-course; capacity & the defined term "
    "'person'; equity of redemption; 1099-C discharge / OID; securitization / "
    "PSA standing; FHA subrogation -- and turn the label back on counsel. Cite "
    "ONLY authority that actually appears in the corpus grounding or the "
    "record; if a proposition has no such support, drop it -- never invent "
    "one.\n"
    "5. Write in formal federal motion style: numbered paragraphs, "
    "third-person reference to plaintiff, no first-person 'I'.\n"
    "6. If an assertion cannot be grounded in the input, write "
    "'[FACT NOT IN RECORD - DRAFTER MUST VERIFY]' rather than inventing.\n"
    "7. Do not advise the plaintiff or include commentary; produce only "
    "the requested motion sections.\n"
    "8. DEFINITIONS DISCIPLINE -- make the draft airtight against an ambiguity / "
    "'failure to state a claim' attack: the FIRST time you use a weight-bearing "
    "term (each party, each instrument, each statutory term of art), define it "
    "inline as  Full Name (\"DefinedTerm\")  and then use that EXACT defined term "
    "EVERY time after. NEVER vary it with a synonym or pronoun -- not 'the bank,' "
    "'the lender,' 'the servicer,' 'the company,' 'it,' or 'they'; name the defined "
    "party. For each statutory term of art (e.g. 'debt collector,' 'holder in due "
    "course,' 'person entitled to enforce,' 'qualified written request'), state its "
    "element and cite the controlling section (e.g. 15 U.S.C. 1692a(6); UCC 3-301). "
    "Plead ONE actor and ONE fact per sentence, active voice, facts before legal "
    "conclusions, so each element is unambiguous on its face.\n"
    "9. CONCISENESS & IMPACT -- brevity is persuasion; reduce page count while hitting where it hurts. "
    "LEAD with the most dispositive grounds first (jurisdictional / void / non-waivable / binding-precedent), "
    "and make each point ONCE with its SINGLE best-connected CONTROLLING authority -- prefer the BINDING, "
    "most co-cited authority the grounding surfaces over stacking parallel cites. Do NOT restate a proposition "
    "across sections; do NOT string-cite three cases where one binding case controls; do NOT recite background "
    "the court already has. Do NOT DROP an operative ground -- an unraised ground can be deemed WAIVED or "
    "forfeited; RAISE every ground (concisely if settled -- see rule 11), and vary the DEPTH, not the coverage: "
    "a weak or collateral ground gets ONE tight sentence + its cite, the dispositive grounds get full "
    "development. Cut only genuine REDUNDANCY (the SAME point restated) and background the court already has -- "
    "never a distinct ground. A tight motion that RAISES every ground but only DEVELOPS the two or three that "
    "WIN beats a long one that buries them AND beats a short one that waives them. (This does NOT relax rules "
    "1-8: every point stays grounded and every term stays defined.)\n"
    "10. ESTABLISH, DO NOT ARGUE -- foreclose MERIT and DISCRETION so neither can be used against plaintiff. "
    "Persuasive prose smuggles in a PRESUMPTION the court can attack; instead STATE each proposition and PROVE "
    "it with authority. For each NOVEL, CONTESTED, or DISPOSITIVE operative point, lay the AUTHORITY STACK in "
    "descending force -- U.S. Constitution -> public law / Statutes at Large -> controlling statute (quote its "
    "operative 'shall/must/void' text) -> BINDING precedent (quote its HOLDING, what the case actually held, "
    "not just the cite) -> court rule -> equity maxim -- including ONLY the layers that APPLY (padding "
    "inapplicable authority is overkill and weakens). OPEN each such section with the FULL controlling "
    "provision + the binding holding that FORECLOSES a narrow reading, so the court cannot sidestep by "
    "cherry-picking a subsection: close the gap FIRST, then apply it. Anchor every consequence to MANDATORY / "
    "non-waivable / jurisdictional / void authority ('shall', 'must', 'is void', 'lacks subject-matter "
    "jurisdiction', 'cannot be waived') so NO discretion remains. Plead each ELEMENT airtight (defined term -> "
    "corroborated fact -> element satisfied) so NO 'failure to state a claim' gap survives. A three-page motion "
    "that forecloses every gap beats an eleven-page one that argues around them.\n"
    "11. CITE THE SETTLED, ARGUE ONLY THE NOVEL (proof-of-facts compactness). EVERY operative point is still "
    "RAISED and CITED -- raising it (even in one line) is what PRESERVES it; an unraised point is what gets "
    "WAIVED. What varies is DEPTH, never coverage. MOST propositions in a filing are SETTLED law -- they need "
    "a CITATION, not an argument. State a settled proposition in ONE declarative sentence and drop its "
    "controlling authority to a FOOTNOTE, then move on; do NOT develop the authority stack (rule 10) for law "
    "the court already knows -- but DO state it, so it is on the record and preserved. Reserve the full stack "
    "and developed ARGUMENT for the FEW "
    "points that are genuinely NOVEL or CONTESTED, or where the court has DIVERGED from the controlling text -- "
    "there, argue in full. Build the body in proof-of-facts style: ELEMENT -> corroborated RECORD fact (cite "
    "'R.__a'/docket) -> controlling authority in a footnote -> element satisfied; one tight line per element, "
    "the citations carried in the footnotes, not the prose. This is the compact format: the body reads as a "
    "clean chain of established elements, and argument appears only where the law is actually in dispute. Do "
    "NOT restate settled black-letter law in paragraphs of prose; a rebuttal or complaint that cites the "
    "settled and argues only the new is SHORTER and STRONGER than one that argues everything.\n"
    "12. ZERO-WAIVER REBUTTAL MATRIX (replies/oppositions only) -- when the input carries an opposing "
    "brief's argument headings (rebuttal_matrix), answer EVERY heading 1:1 in their order: a short bold "
    "heading naming THEIR point, then rules 9-11 depth control (settled counter = one sentence + footnote "
    "cite; dispositive counter = developed). NEVER merge two of their points into one rebuttal; NEVER skip "
    "one -- an unanswered point reads as conceded. CLOSE each rebuttal by naming the mandatory duty the "
    "court must perform ('shall/must' text) so no discretionary exit remains. Keep each rebuttal CONCISE -- "
    "the matrix guarantees coverage, rules 9-11 guarantee brevity; a confused judge rules against the "
    "confuser. Where the grounding provides an Am. Jur. Proof of Facts element chain, use its element list "
    "as the rebuttal's spine (element -> record fact -> footnote cite).\n"
    "13. SUBSTANTIVE-FACTS RESPONSE DEMAND -- whenever the instrument demands a response, answers, or "
    "compliance from ANY actor (demand tables, notices, interrogatory-style items, meet-and-confer "
    "requirements), state that each response must address the SUBSTANTIVE FACTS presented -- the specific "
    "documents, dates, amounts, and instruments of record -- and that a response addressing "
    "characterizations while leaving the underlying facts unengaged is a NON-RESPONSE: each fact not "
    "specifically and substantively denied with supporting evidence stands ADMITTED (Fed. R. Civ. P. "
    "8(b)(6) in federal instruments; Pa.R.C.P. 1029(b) in Pennsylvania instruments). Pair with the "
    "substantial-evidence standard where a denial or order is invoked (5 U.S.C. 706(2)(E); Kelly v. R.R. "
    "Ret. Bd., 625 F.2d 486 (3d Cir. 1980)).\n"
    "14. AUTHORITY-DISCLOSURE DEMAND -- in any instrument challenging or demanding justification for an "
    "act by a public officer, court, agency, or counsel, demand that the response (and any order or "
    "determination relied upon) IDENTIFY THE ENACTED AUTHORITY under which the specific act was taken: "
    "the statute (traced to its Public Law / Statutes at Large origin where codification is disputed), "
    "rule, or canon that authorizes it. Ground: public officers exercise only delegated authority; an act "
    "whose authorizing enactment cannot be named carries no presumption of validity (5 U.S.C. 706(2)(C) -- "
    "'in excess of statutory jurisdiction, authority, or limitations'). Where the challenged act is a "
    "referral-style ruling ('see prior order'), demand identification BY PAGE AND LINE of where the "
    "referenced order answers the specific question presented.\n"
    "15. WAIVER SYMMETRY -- in every instrument that faces or anticipates a waiver/forfeiture argument, "
    "and in every notice or demand: (a) quote the definition -- waiver is 'an intentional relinquishment "
    "or abandonment of a known right or privilege,' Johnson v. Zerbst, 304 U.S. 458, 464 (1938); (b) "
    "apply it SYMMETRICALLY: defenses, objections, and denials the OPPOSING actors do not raise within "
    "the response period are WAIVED under the same standard they invoke; (c) foreclose it DEFENSIVELY: no "
    "waiver by plaintiff may be presumed from silence, ambiguity, or participation under protest -- waiver "
    "must be PROVEN as an intentional relinquishment of a KNOWN right by the party asserting it, and all "
    "rights are expressly reserved (1 Pa.C.S. 1308). Where the record shows the opposing side deploying "
    "waiver walls against non-waivable grounds (e.g. SMJ under Arbaugh v. Y&H Corp., 546 U.S. 500), name "
    "that conduct and hold them to waiver's exact definition. "
    "16. WHO-DOES-THIS-REACH (defined-class resolution) -- before pleading ANY statute or rule, resolve "
    "WHO it reaches from the statute's OWN definitions section, falling back to the general definitions "
    "(1 U.S.C. 1 Dictionary Act federally [corpus: usc_t1_dictionary_act]; 1 Pa.C.S. 1991 in PA [corpus: "
    "pa_1_construction_definitions]): does 'person' in THIS statute include a corporation, and in what "
    "capacity is EACH party reached -- individual, corporate entity, officer, fiduciary, surety? Apply "
    "the includes-rule: 'means' = closed class; 'includes' expands ONLY where the text supplies an "
    "expansion clause (26 U.S.C. 7701(c) 'shall not be deemed to exclude' [corpus: usc_t26_s7701_full]); "
    "otherwise the enumerated items define the class. State the resolved class in the instrument when "
    "coverage could be contested; never plead a statute against a party outside its defined reach. "
    "17. AUTHORITY-AND-JURISDICTION LINE -- every count and every demand names three things: (a) the "
    "ENACTING authority behind the provision invoked (Congress/General Assembly + the public-law or "
    "code source); (b) the FORUM whose jurisdiction reaches THIS defendant in THIS capacity (subject "
    "matter + personal, with the statutory grant); (c) the CAPACITY nexus -- why this defendant, in the "
    "capacity resolved under Rule 16, is within that forum's reach. No count may rest on an unexamined "
    "jurisdictional assumption; where the opposing side's paper rests on one, name it as a defect.\n"
    "18. REMEDY-RUNG DISCIPLINE -- every instrument that responds to a judicial act (order, dismissal, "
    "silence on a motion) names its RUNG and stays in it; mixing rungs is how relief gets denied. The "
    "ladder, each with its statutory vehicle: (R1) same-court correction -- reconsideration/Rule 59(e)/"
    "objections: quote the enacted duty ('shall' text) beside the order's contrary language, demand a "
    "written ruling on THAT duty; (R2) appeal -- divergence from statutory text is LEGAL ERROR reviewed "
    "de novo, no deference (28 U.S.C. 1291; Pa.R.A.P.); (R3) mandamus (28 U.S.C. 1651) -- ONLY for "
    "refusal of a nondiscretionary duty: plead the duty as written+mandatory, the refusal, and no other "
    "adequate remedy; (R4) void-judgment attack (Fed.R.Civ.P. 60(b)(4) / collateral) -- ONLY where the "
    "act was outside the court's granted jurisdiction (trace the grant to its enacted charter; a void "
    "judgment has no clock); (R5) recusal (28 U.S.C. 144/455 or state analog) -- documented bias "
    "pattern, affidavit-supported, removes the judge prospectively but corrects no past order; (R6) "
    "conduct complaint (28 U.S.C. 351 / Pa. JCB) -- runs in PARALLEL, never pleaded inside a case "
    "instrument as a ground for relief. RULES: (a) the instrument states which rung it invokes and the "
    "rung's own vehicle/standard; (b) never plead a damages claim against a judicial officer for a "
    "judicial act (immunity forecloses it -- the strong move is R1-R4 against the ORDER, and saying so "
    "preempts the 'disgruntled litigant' frame); (c) every rung-1 instrument BUILDS the rung-2 record: "
    "the divergence (statute text vs order text, side by side) must appear IN the filing so it is "
    "preserved -- an undocumented divergence is unappealable; (d) rung selection follows the defect: "
    "wrong-on-the-law -> R1/R2; won't-act-on-mandatory-duty -> R3; acted-without-granted-power -> R4; "
    "pattern-of-bias -> R5+R6 in parallel with R1/R2."
)


# ---------- helpers ----------
def case_id_to_folder(case_id):
    if case_id.startswith('fed:'):
        parts = case_id.split(':')
        if len(parts) >= 3:
            return f"fed_{parts[2]}"
    if case_id.startswith('phila_ccp:'):
        return f"phila_ccp_{case_id.split(':', 1)[1]}"
    return case_id.replace(':', '_').replace('/', '_')


def find_stubs(case_id=None, chain=None, priority=None, packet_dir=None):
    """Locate stub .md files to process."""
    if packet_dir:
        base = Path(packet_dir)
    elif case_id:
        base = (REPO / 'obsidian_sync' / 'cases' / case_id_to_folder(case_id)
                / 'drafts' / '_bundle11_packet')
    else:
        return []

    motions_dir = base / 'motions'
    if not motions_dir.exists():
        return []

    stubs = []
    for chain_dir in sorted(motions_dir.iterdir()):
        if not chain_dir.is_dir():
            continue
        if chain is not None:
            # match "chain_01_..." against chain number
            m = re.match(r'chain_(\d+)_', chain_dir.name)
            if not m or int(m.group(1)) != chain:
                continue
        for stub in sorted(chain_dir.glob('*.md')):
            if stub.name.endswith('_DRAFT.md'):
                continue
            if priority is not None:
                m = re.match(r'(\d+)_', stub.name)
                if not m or int(m.group(1)) != priority:
                    continue
            stubs.append(stub)
    return _dedup_case_level_stubs(stubs)


# === CASE_LEVEL_FILINGS taxonomy (Batch 4, 2026-06-13) ====================
# Filings that are case-level by definition: ONE per case, never per-chain.
# When the upstream stub generator emits chain-specific copies, we keep
# ONE stub per (case, filing_type), preferring chain_99_m0_selected/.
# The companion CASE_LEVEL_FIX_V1 wrapper at end-of-file appends
# consolidation guidance to the LLM prompt for these types.
CASE_LEVEL_FILINGS = frozenset({
    # Comprehensive / cross-party
    'omnibus_exposure_filing',
    # Vacatur / void-order / case-wide reconsideration
    'rule_60b_motion',
    'rule_60b_motion_1to3',
    'rule_60b4_motion',
    'rule_60d3_fraud_on_court',
    'motion_to_vacate',
    'motion_to_vacate_void_order',
    'motion_for_reconsideration',
    'motion_for_reconsideration_hall_order',
    'motion_to_certify_54b',
    'rule_54b_certification',
    'amended_complaint',
    # Removal / remand (Rule 1447 is case-wide)
    'motion_to_remand',
    # Appellate (one per appeal/court)
    'scotus_petition_for_certiorari',
    'motion_to_reinstate_appeal',
    'motion_to_recall_mandate',
    'frap_40_panel_rehearing',
    'frap_40_rehearing_en_banc',
    'frap_21_petition_for_writ_of_mandamus',
    'petition_for_writ_of_mandamus',
    'appellate_brief_3rd_cir',
    'appellate_reply_brief',
    'notice_of_supplemental_authority',
    'notice_of_appeal_reserved',
    # Judicial misconduct (about ONE judge per case)
    'judicial_council_complaint_28_usc_351',
    'section_1983_judicial_misconduct_complaint',
    'motion_for_recusal',
    'motion_for_disqualification_hall',
    'pa_jcb_complaint_judge_hall',
    # Garnishment-scope / NT (case-wide writ semantics)
    'motion_for_clarification_writ_scope',
    'motion_to_reissue_writ_of_execution',
    'renewed_motion_to_compel_northern_trust',
    'motion_for_sanctions_northern_trust',
    'pa_odc_complaint_nt_counsel',
})


def _filing_type_from_stem(stem):
    """Derive canonical filing_type from a stub filename stem.
    Mirrors the normalization at L1026-1034 of process_stub."""
    name = (stem or '').lower()
    if name.endswith('_draft'):
        name = name[:-6]
    while name and name[0].isdigit():
        name = name[1:]
    if name.startswith('_'):
        name = name[1:]
    for _suf in ('_fed_', '_phila_ccp_', '_3d_cir_', '_scotus_', '_pa_super_'):
        if _suf in name:
            name = name.split(_suf)[0]
            break
    return name


def _case_slug_from_stub(path):
    """Extract case slug (e.g. 'fed_26-cv-00123') from a stub Path."""
    try:
        parts = Path(path).parts
        idx = parts.index('cases')
        return parts[idx + 1]
    except (ValueError, IndexError):
        return ''


def _dedup_case_level_stubs(stubs):
    """For filing types in CASE_LEVEL_FILINGS, keep ONE stub per
    (case, filing_type). Prefer chain_99_m0_selected/ when present,
    else lowest-numbered chain. Chain-level filings pass through
    untouched."""
    # Walk stubs in a deterministic preference order so first-seen wins
    def _pref(stub_path):
        parent = stub_path.parent.name
        return (0 if parent.startswith('chain_99_') else 1, parent, stub_path.name)
    ordered = sorted(stubs, key=_pref)

    seen_keys = set()
    keep_paths = set()
    n_collapsed = {}  # (case, ft) -> [original_paths]
    for stub in ordered:
        ft = _filing_type_from_stem(stub.stem)
        if ft in CASE_LEVEL_FILINGS:
            case_slug = _case_slug_from_stub(stub)
            key = (case_slug, ft)
            if key in seen_keys:
                n_collapsed.setdefault(key, []).append(str(stub))
                continue
            seen_keys.add(key)
            n_collapsed.setdefault(key, []).append(str(stub))
        keep_paths.add(str(stub))

    # Preserve original ordering for the kept set
    filtered = [s for s in stubs if str(s) in keep_paths]
    n_dropped = len(stubs) - len(filtered)
    if n_dropped > 0:
        # Summary: how many duplicates per (case, ft)
        collapsed_keys = [k for k, v in n_collapsed.items() if len(v) > 1]
        print(f"[case-level dedup] dropped {n_dropped} duplicate chain-stubs "
              f"across {len(collapsed_keys)} (case, filing_type) pair(s)")
        for k in sorted(collapsed_keys):
            print(f"  consolidated: {k[0]} :: {k[1]}  "
                  f"(kept 1 of {len(n_collapsed[k])})")
    return filtered


def _is_case_level_filing(motion_filename):
    """True iff motion_filename's normalized filing_type is in
    CASE_LEVEL_FILINGS. Used to gate SCOPE_CLAMP_V1 and trigger
    CASE_LEVEL_FIX_V1."""
    if not motion_filename:
        return False
    stem = str(motion_filename).rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return _filing_type_from_stem(stem) in CASE_LEVEL_FILINGS
# === END CASE_LEVEL_FILINGS taxonomy ====================================


# === STUB_SANITIZE_V1 (Batch 5a, 2026-06-13) ==============================
# Strip placeholder tokens that the upstream stub generator left in the
# stub text. These are TODO markers ("Complaint + status", "[record]",
# etc.) that were never substituted. If the LLM sees them, it
# reproduces them verbatim and the leak gate flags the resulting draft.
#
# Hook point: process_stub, immediately after stub_text = stub.read_text(...)
# Anything below that point (build_user_prompt, _section_prompt,
# _extract_active_defendant, schema validator, etc.) sees sanitized text.
_STUB_SANITIZE_PATTERNS = [
    # "Complaint + status" — fed_06077 chain_01_state_farm (10 stubs hit)
    (re.compile(r"Complaint\s*\+\s*status", re.IGNORECASE), ""),
    # Bracketed scalar placeholders the stub generator emits as TODOs
    (re.compile(r"\[record\]", re.IGNORECASE), ""),
    (re.compile(r"\[date\]", re.IGNORECASE), ""),
    (re.compile(r"\[citation\]", re.IGNORECASE), ""),
    (re.compile(r"\[exhibit\]", re.IGNORECASE), ""),
    (re.compile(r"\[source\]", re.IGNORECASE), ""),
    (re.compile(r"\[case[\s_]?name\]", re.IGNORECASE), ""),
    # TODO markers
    (re.compile(r"\[(?:TODO|todo)\]|<TODO>", re.IGNORECASE), ""),
    # Italic placeholder: "See *Smith + holding*" style markers
    (re.compile(r"See \*[A-Z][a-z]+(?: \+ [a-z]+)+\*?"), ""),
]


def _sanitize_stub_text(text):
    """Strip placeholder tokens from stub text before downstream use.
    Returns (sanitized_text, hits_dict). hits_dict maps pattern -> count
    for logging. Empty hits_dict means stub was clean."""
    if not text:
        return text, {}
    hits = {}
    for pat, repl in _STUB_SANITIZE_PATTERNS:
        new_text, n = pat.subn(repl, text)
        if n > 0:
            hits[pat.pattern] = n
            text = new_text
    # Collapse any double spaces created by the substitutions
    text = re.sub(r"[ \t]+", " ", text)
    # Strip empty parens / brackets left behind: "( )" "()" "[ ]"
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    # Restore spacing at line boundaries
    text = re.sub(r" +\n", "\n", text)
    return text, hits
# === END STUB_SANITIZE_V1 ================================================


# === APPELLATE_CAPTION_V1 helper ===
def _lookup_appeal_number(case_id):
    """Best-effort: read appeal_number from the case's CASE_GROUND_TRUTH.yaml.
    Returns None on any failure (caption falls back to 'No. ____')."""
    try:
        if not case_id or not case_id.startswith('fed:'):
            return None
        slug = 'fed_' + case_id.split(':')[-1]
        gt = REPO / 'obsidian_sync' / 'cases' / slug / 'meta' / 'CASE_GROUND_TRUTH.yaml'
        if not gt.exists():
            return None
        import re as _r
        for line in gt.read_text(encoding='utf-8', errors='ignore').splitlines():
            m = _r.match(r'\s*appeal_number:\s*(\S+)', line)
            if m:
                return m.group(1).strip().strip('\'"')
    except Exception:
        pass
    return None
# === END APPELLATE_CAPTION_V1 helper ===

# === APPELLATE_CAPTION_V1: filing_type decides trial vs appellate court ===
_APPELLATE_FILING_TYPES = {
    'appellate_brief_3rd_cir', 'appellate_reply_brief',
    'frap_21_petition_for_writ_of_mandamus', 'petition_for_writ_of_mandamus',
    'frap_40_panel_rehearing', 'frap_40_rehearing_en_banc',
    'motion_to_recall_mandate', 'motion_to_reinstate_appeal',
}
_SCOTUS_FILING_TYPES = {'scotus_cert_petition'}
_PA_SUPER_FILING_TYPES = {'appellate_brief_pa_super', 'appellate_brief_superior_court',
                          'appellate_reply_brief_pa_super'}

# Appellate PRINCIPAL briefs get the error-correction, Rule-2111/FRAP-28 structure (NOT motion sections).
_APPELLATE_BRIEF_FILING_TYPES = (_PA_SUPER_FILING_TYPES
                                 | {'appellate_brief_3rd_cir', 'appellate_reply_brief'}
                                 | {'scotus_cert_petition'})
# Per-court appellate anchors: (court label, jurisdiction cite, structure rule, standard-of-review rule).
_APPELLATE_RULES = {
    'PA_SUPER': ("the Superior Court of Pennsylvania", "42 Pa.C.S. § 742; Pa.R.A.P. 341, 903",
                 "Pa.R.A.P. 2111", "Pa.R.A.P. 2119"),
    '3D_CIR': ("the United States Court of Appeals for the Third Circuit", "28 U.S.C. § 1291; Fed. R. App. P. 3, 4",
               "Fed. R. App. P. 28", "Fed. R. App. P. 28(a)(8)"),
}

# NOTE: the appeal-vehicle DECISION + jurisdictional-cite map moved to pipeline/m0_appeal_gate.py
# (single source of truth: the LLM gate decides the vehicle from posture + corpus; the cite map is
# reference data there). The drafter CONSUMES that decision in _draft_appellate_sectioned.


def _appellate_court_for(motion_filename):
    """Return the appellate court id ('PA_SUPER'/'3D_CIR'/'SCOTUS') for an appellate brief filename, else ''.

    Prefix-tolerant (2026-07-10): a stub named 'appellate_brief_3rd_cir_opening' (or '_principal',
    '_reply', etc.) still resolves to its court. The drafter routes by filing FAMILY, not an exact
    suffix match — otherwise an '_opening' suffix silently drops the brief onto the MOTION path and
    m0 renders motion sections instead of the FRAP/Rule-2111 appellate structure. PA Superior is
    checked FIRST because its reply-brief name is a prefix-superset of the federal one."""
    ft = _filing_type_from_stem(Path(str(motion_filename)).stem.replace('_DRAFT', '').replace('_FILING', ''))
    # Prefix checks use the RAW stem, not ft: _filing_type_from_stem() strips '_pa_super_' as a
    # case-slug infix, which would mangle 'appellate_brief_pa_super_opening' down to 'appellate_brief'.
    raw = Path(str(motion_filename)).stem.lower().replace('_draft', '').replace('_filing', '').lstrip('0123456789_')
    if ft in _PA_SUPER_FILING_TYPES or any(raw.startswith(p) for p in (
            'appellate_brief_pa_super', 'appellate_brief_superior_court', 'appellate_reply_brief_pa_super')):
        return 'PA_SUPER'
    if ft in _SCOTUS_FILING_TYPES or raw.startswith('scotus_cert_petition'):
        return 'SCOTUS'
    if raw.startswith('appellate_brief_3rd_cir') or raw.startswith('appellate_reply_brief'):
        return '3D_CIR'
    return ''

def _is_appellate_filing(motion_filename):
    return bool(_appellate_court_for(motion_filename))

def caption_for_case(case_id, filing_type=None, appeal_number=None):
    """Return court caption header tied to case_id AND filing_type.
    Appellate filing types route to the Court of Appeals (or SCOTUS / PA Superior)
    caption with the appeal docket; everything else keeps the trial-court caption."""
    ft = (filing_type or '').strip()
    if ft in _SCOTUS_FILING_TYPES:
        return {'court': 'IN THE SUPREME COURT OF THE UNITED STATES',
                'docket': (f"No. {appeal_number}" if appeal_number else 'No. ____')}
    if ft in _APPELLATE_FILING_TYPES:
        return {'court': 'IN THE UNITED STATES COURT OF APPEALS '
                         'FOR THE THIRD CIRCUIT',
                'docket': (f"No. {appeal_number}" if appeal_number else 'No. ____')}
    if ft in _PA_SUPER_FILING_TYPES:
        return {'court': 'IN THE SUPERIOR COURT OF PENNSYLVANIA',
                'docket': (f"No. {appeal_number}" if appeal_number else 'No. ____')}
    if case_id.startswith('fed:'):
        # fed:2:26-cv-00123 -> E.D. Pa., No. 2:26-cv-00123
        parts = case_id.split(':')
        if len(parts) >= 3:
            return {
                'court': 'UNITED STATES DISTRICT COURT FOR THE EASTERN '
                         'DISTRICT OF PENNSYLVANIA',
                'docket': f"Civil Action No. {parts[2]}",
            }
    if case_id.startswith('phila_ccp:'):
        n = case_id.split(':', 1)[1]
        return {
            'court': 'COURT OF COMMON PLEAS OF PHILADELPHIA COUNTY, '
                     'PENNSYLVANIA',
            'docket': f"No. {n}",
        }
    return {'court': '[COURT - TO BE FILLED]', 'docket': f'No. {case_id}'}


def extract_case_id_from_stub(stub_path):
    """Walk up to find the parent case directory."""
    p = Path(stub_path).resolve()
    for parent in p.parents:
        if parent.name.startswith('fed_') or parent.name.startswith('phila_ccp_'):
            n = parent.name
            if n.startswith('fed_'):
                # fed_26-cv-00123 → fed:2:26-cv-00123 (assume district 2)
                # We can't recover the district number reliably; use literal
                return f"fed:2:{n[4:]}"
            if n.startswith('phila_ccp_'):
                return f"phila_ccp:{n[10:]}"
    return None


# ---------- LLM call ----------
def call_llm(user_prompt, system=SYSTEM_PROMPT,
             max_tokens=8000, temperature=0.2, retries=2, enable_thinking=None):
    """Call llama-server OpenAI-compatible /v1/chat/completions endpoint.

    The server runs Qwen3.6-35B-A3B (Qwen3 family, a thinking model). We append
    the '/no_think' chat-template switch to the system turn so the model emits
    the motion directly rather than narrating its reasoning scratchpad (which
    previously leaked into the body and starved the actual motion of tokens).
    max_tokens raised to 8000 so a full motion (facts + argument + prayer)
    completes without truncation."""
    # Qwen3 thinking-mode suppression. This llama-server build honors the
    # chat_template_kwargs switch (NOT '/no_think' in the prompt — that was
    # tested and ignored). enable_thinking=False makes the model emit the
    # motion directly with no reasoning narration.
    # === CALL_LLM_VIA_LAYER_V1 ===
    # /v1/chat/completions is broken with this server's patched Jinja template:
    # output lands in 'reasoning_content', not 'content', so reasoning leaked
    # into the body (71k bloat, malformed </think>, duplicate headers).
    # llm_layer.chat() uses the native /completion endpoint with manual ChatML
    # and returns clean content. Route through it (thinking ON, no leak) -- the
    # same path every clean filing used. max_tokens maps to n_predict.
    # === AGNOSTIC_LLM_READ_V1 ===
    # Thinking stays ON (airtight reasoning). Call /v1/chat/completions and read
    # message.content: with --reasoning-format deepseek the server separates the
    # scratchpad (reasoning_content) from the finished motion (content), so there
    # is nothing to strip. Model-agnostic by API contract. Fallback to a
    # STRUCTURAL cut of reasoning_content only if content is empty.
    last_err = None
    # THINKING SWITCH: motions keep thinking ON (server separates scratchpad via --reasoning-format
    # deepseek). But a complex prompt can push the model to dump its plan into `content` anyway. For
    # those callers, enable_thinking=False forces the answer into `content` with nothing to leak —
    # the SAME fix m0_rebuttal_gate / m0_appeal_gate use. Default None = leave server default (thinking on).
    # THINKING TOGGLE (A/B, non-breaking): env M0_DRAFT_THINKING overrides the caller — 'off' forces the drafter
    # to ASSEMBLE from grounding with no reasoning narration (fast path A/B); 'on' forces reasoning; unset keeps
    # the caller's value (current live default). Lets us produce a thinking-off draft and quality-compare it to
    # the thinking-on reference WITHOUT changing the live path until the fast path is proven >= reference.
    _tenv = os.environ.get("M0_DRAFT_THINKING", "").strip().lower()
    if _tenv in ("off", "false", "0", "no"):
        enable_thinking = False
    elif _tenv in ("on", "true", "1", "yes"):
        enable_thinking = True
    _payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if enable_thinking is not None:
        _payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{LLAMA_URL}/v1/chat/completions",
                json=_payload,
                timeout=600,
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if content:
                return content
            # FALLBACK (future-model safety): server put everything in
            # reasoning_content. Structurally cut to the LAST mandated header —
            # anchors on OUR output contract, not the model's reasoning phrases.
            rc = (msg.get("reasoning_content") or "").strip()
            if rc:
                hdrs = list(re.finditer(
                    r'^\s*#{1,4}\s*(STATEMENT OF FACTS|ARGUMENT|PRAYER FOR RELIEF|INTRODUCTION)\b',
                    rc, re.I | re.M))
                return rc[hdrs[-1].start():] if hdrs else rc
            return ""
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"    [retry {attempt + 1}] {e}", file=sys.stderr)
                time.sleep(2)
    raise last_err


# ---------- prompt builder ----------
def _baked_grounding_for(stub_text):
    """B: the analysis-baked corpus-retrieval grounding for this filing's case (canon/restoration/pseudolaw/
    definition), or None -> retrieve live. The analysis gate (case_facts_refresh) pre-computes it so the drafter
    doesn't do the ~2-min corpus retrieval at draft time. Never raises."""
    try:
        from pipeline import grounding_cache as _gc
        cid = ""
        try:
            cid = _pf_extract_case_id_v1(stub_text) or ""
        except Exception:
            cid = ""
        if not cid:
            m = re.search(r"(?:case_id|canonical_id)[\"'\s:=]+([A-Za-z]+:[\w:.\-]+)", stub_text or "")
            cid = m.group(1) if m else ""
        return _gc.read(cid) if cid else None
    except Exception:
        return None


def build_user_prompt(stub_text, motion_filename):
    """Build the user-turn prompt sent to the LLM."""
    _bk = _baked_grounding_for(stub_text)   # B: analysis-baked grounding (None -> live retrieval, unchanged path)
    try:
        _cg = (_bk.get("canon") if _bk else get_canon_grounding(stub_text)) or ""
    except Exception:
        _cg = ""
    _canon_block = (_cg + "\n") if _cg else (
        "INTERPRETIVE CANONS (comprehension, not a checklist): where a governing "
        "statute, rule, or contract term in the record is contested, apply and NAME the "
        "canon your reading actually supports -- a remedial consumer statute "
        "(FCRA/FDCPA/TILA/RESPA/UTPCPL) construed liberally for the plaintiff; ambiguity "
        "in the defendant's own drafted contract against the drafter (contra "
        "proferentem); 'shall' as a mandatory duty; expressio unius; or de novo reading "
        "with no agency deference (Loper Bright, 603 U.S. 369 (2024)). Mention a canon "
        "ONLY where it genuinely governs the text; none if none does. Grounded UCC/"
        "capacity/redemption/discharge/securitization arguments are permitted where the "
        "corpus supports them; only court-sanctioned sovereign-citizen schemes are barred.\n\n")
    try:
        _restore = (_bk.get("restoration") if _bk else get_restoration_grounding(stub_text)) or ""
    except Exception:
        _restore = ""
    if _restore:
        _canon_block = _restore.rstrip() + "\n\n" + _canon_block
    try:
        _rebut = (_bk.get("pseudolaw") if _bk else get_pseudolaw_rebuttal_grounding(stub_text)) or ""
    except Exception:
        _rebut = ""
    if _rebut:
        _canon_block = _rebut.rstrip() + "\n\n" + _canon_block
    # ADVOCACY STANCE (always injected; m0 decides when each applies — comprehension, not a checklist)
    _canon_block = _canon_block + (
        "\nADVOCACY STANCE (you judge when each applies, never gratuitously):\n"
        "- HIT EVERY POINT directly and by name: address each ground the opposing filing or order raises; "
        "leave nothing unanswered; argue as the party whose allegations these are, to near-perfection.\n"
        "- LOCK-IN: binding authority BINDS the court. Where a contrary ruling would disregard it, you MAY state "
        "on the record that declining to apply the authority necessarily holds the law -- and the rule it "
        "embodies -- beneath the court, opposing counsel, and the defendant, preserving that departure for review "
        "and for the accountability and remedies that follow (28 U.S.C. 351 / judicial-conduct referral, "
        "sanctions, appeal). Deploy it where it strengthens the record; you decide when it is warranted.\n"
        "- DEFINITIONS: carry disputed/defined terms in the Schedule of Definitions (appendix at the end), NOT "
        "redefined in the body; use each defined term mechanically and let the IF/THEN lock it.\n")
    return (
        f"Below is a motion stub for `{motion_filename}` containing all "
        f"controlling authority, opposing authority defects, and "
        f"scanner-corroborated facts available for this motion. Expand it "
        f"into a full motion body covering three sections.\n\n"
        f"RECORD-FACT SCOPE: any stub section headed 'NEW EVIDENCE', 'EVIDENCE "
        f"BLOCK', 'RECORD FACTS', or carrying [doc]/[corpus] source tags is "
        f"scanner-corroborated RECORD FACT, equal in rank to scanner_corroboration "
        f"entries: each such item MUST appear as a numbered STATEMENT OF FACTS "
        f"paragraph (cite its source_doc/exhibit) AND be USED in the argument "
        f"section it supports. Do NOT treat these as background reference; a "
        f"dispositive record fact left out of the body is a drafting failure.\n\n"
        f"INPUT STUB:\n"
        f"---BEGIN STUB---\n"
        f"{stub_text}\n"
        f"---END STUB---\n\n"
        f"Produce output in this EXACT format with these EXACT headers:\n\n"
        f"## STATEMENT OF FACTS\n\n"
        f"[Numbered paragraphs 1. through N. Each numbered paragraph cites "
        f"the source_doc and quotes the scanner-corroborated text directly. "
        f"No facts beyond what appears in the input stub. If a fact is "
        f"needed but not in record, mark "
        f"'[FACT NOT IN RECORD - DRAFTER MUST VERIFY]'.]\n\n"
        f"## ARGUMENT\n\n"
        f"### I. [First argument header — tie to the motion type]\n\n"
        f"[Argument paragraphs. Cite controlling authorities by their exact "
        f"citation strings from the input. Where opposing authority is "
        f"flagged as defective, address the defect (misapplication, "
        f"non-precedential status, etc.) by name.]\n\n"
        f"### II. [Second argument header if the input supports one]\n\n"
        f"[...]\n\n"
        + _canon_block +
        f"## PRAYER FOR RELIEF\n\n"
        f"WHEREFORE, Plaintiff respectfully requests that this Court:\n"
        f"1. [Specific relief tied to the motion type and listed remedies]\n"
        f"2. [Additional relief if supported]\n"
        f"3. Grant such other and further relief as this Court deems just "
        f"and proper.\n\n"
        f"Begin output now, starting with `## STATEMENT OF FACTS`."
    )


# ---------- output assembler ----------

def _clean_llm_output(text, section="facts"):
    """Strip the model's reasoning scratchpad that leaks before the motion body.
    Handles: <think> blocks; truncation to the first '## STATEMENT OF FACTS'
    header (any heading level, or bold); and, when no header exists, cutting a
    known reasoning preamble by anchoring on the first real motion content
    (a numbered fact paragraph). Never silently empties — if no anchor is
    found, keeps text behind a visible REVIEW marker."""
    import re as _re
    if not text:
        return text
    # === STRIP_BINDING_TAGS_V1: remove stub authority annotations that the
    # model copies into prose, e.g. "(binding: BINDING_FED_RULE)". Leave the
    # surrounding authority text intact. ===
    text = _re.sub(r"\s*\((?:binding|status)\s*:\s*[A-Z0-9_ ]+\)", "", text)
    # === END STRIP_BINDING_TAGS_V1 ===
    # 1. remove <think>...</think> blocks
    text = _re.sub(r'<think>.*?</think>', '', text, flags=_re.IGNORECASE | _re.DOTALL)
    # === THINK_HARDEN_V1: thinking=True quality mode may emit a stray closing
    # </think> (no opener) or post-</think> planning narration. The real answer
    # always follows the LAST </think>; cut to it. Then drop any leading
    # planning preamble up to the first real section header. ===
    _lc = text.lower().rfind('</think>')
    if _lc != -1:
        text = text[_lc + len('</think>'):]
    _pre = _re.search(r"(here'?s a thinking process|here is a thinking process|"
                      r"self-correction|analyze user input|deconstruct (the|requirements))",
                      text, flags=_re.IGNORECASE)
    if _pre:
        _hdr = _re.search(r'^#{0,6}\s*(INTRODUCTION|STATEMENT OF FACTS|ARGUMENT|PRAYER FOR RELIEF|'
                          r'[IVX]+\.\s+[A-Z])', text,
                          flags=_re.IGNORECASE | _re.MULTILINE)
        if _hdr and _hdr.start() > _pre.start():
            text = text[_hdr.start():]
    # === THINK_HARDEN_V2: the preamble may appear WITHOUT a </think> close
    # (e.g. "Here's a thinking process:" dumped raw). Cut forward to the first
    # real section header regardless of </think>. ===
    _pre2 = _re.search(r"(here'?s a thinking process|here is a thinking process|"
                       r"thinking process:|self-correction|analyze user input|"
                       r"deconstruct (the|requirements)|^\s*1\.\s+\*\*)",
                       text, flags=_re.IGNORECASE | _re.MULTILINE)
    # ASSEMBLED_GUARD_V1: the aggressive last-header cut is ONLY safe for a
    # single leaked section (the "facts" section dumps reasoning). On the
    # assembled multi-section body (section="assembled") it would cut to the
    # LAST header (PRAYER) and drop intro+facts+argument. Skip it there.
    if _pre2 and section in (None, "facts"):
        # INDENTED_HEADER_FIX_V1: the model may indent headers (they sit inside a
        # reasoning list) -> allow leading whitespace before the hashes. And the
        # header is often QUOTED in the reasoning before the real output, so cut
        # to the LAST real header occurrence, not the first.
        _hdr_pat = (r'^\s*#{1,6}\s*(INTRODUCTION|STATEMENT OF FACTS|ARGUMENT|'
                    r'PRAYER FOR RELIEF)\b')
        _matches = list(_re.finditer(_hdr_pat, text, flags=_re.IGNORECASE | _re.MULTILINE))
        # keep only headers that come AFTER the reasoning preamble starts
        _after = [m for m in _matches if m.start() > _pre2.start()]
        if _after:
            text = text[_after[-1].start():]
    # === END THINK_HARDEN_V2 ===
    # === PER_SECTION_CLEAN_V1: ARGUMENT/PRAYER have no STATEMENT OF FACTS
    # anchor; for them, <think>+binding strip is the whole job. Never emit the
    # facts-anchor REVIEW warning for non-facts sections. ===
    if section and section != "facts":
        return text.strip()
    # === END PER_SECTION_CLEAN_V1 ===
    # 2. truncate to a real STATEMENT OF FACTS header (#-style)
    m = None
    _all = list(_re.finditer(r'^\s*#{1,6}\s*STATEMENT OF FACTS\b.*$', text,
                             flags=_re.IGNORECASE | _re.MULTILINE))
    if _all:
        # cut to the LAST occurrence (earlier ones may be quoted in reasoning)
        return text[_all[-1].start():].lstrip()
    # 2b. bold-style "**STATEMENT OF FACTS**"
    m = _re.search(r'^\**\s*STATEMENT OF FACTS\b.*$', text,
                   flags=_re.IGNORECASE | _re.MULTILINE)
    if m:
        return text[m.start():].lstrip()
    # 3. no header — cut a known reasoning preamble. Find where real content
    #    begins: the first numbered factual paragraph ("1. The Court ...") that
    #    is NOT part of a planning list. Planning lists use "1.  **Bold**" or
    #    appear under "thinking process"/"Analyze"/"Deconstruct" lines.
    reasoning_markers = (
        "here's a thinking process", "here is a thinking process",
        "analyze user input", "deconstruct requirements", "deconstruct the",
        "let me think", "thinking process:", "step 1:", "first, i",
        # REASONING_MARKERS_V2: chain-of-thought that addresses the prompt
        # directly ("the user wants me to write...") and self-correction asides
        # ("wait, the motion is...", "let me draft"). Without these, leaked
        # planning passes through and lands in the filing body verbatim.
        "the user wants me to", "let's review", "let's draft", "let's look",
        "wait, the prompt", "wait, the motion", "wait, the stub",
        "i need to use only", "i should state", "the stub says:",
        "the case audit mentions", "let me draft",
    )
    low = text.lower()
    has_reasoning = any(mk in low for mk in reasoning_markers)
    if has_reasoning:
        # find the first plausible real section/content anchor AFTER the preamble
        anchors = [
            _re.search(r'^#{1,6}\s*(ARGUMENT|PRAYER FOR RELIEF|INTRODUCTION|'
                       r'PRELIMINARY STATEMENT|JURISDICTION)\b.*$', text,
                       flags=_re.IGNORECASE | _re.MULTILINE),
            # a numbered fact paragraph that references the record
            _re.search(r'^\s*1\.\s+(The\s|Plaintiff\s|On\s|Defendant\s)',
                       text, flags=_re.MULTILINE),
        ]
        cands = [a.start() for a in anchors if a]
        if cands:
            cut = min(cands)
            return ("## STATEMENT OF FACTS\n\n" + text[cut:].lstrip()
                    if not text[cut:].lstrip().upper().startswith(("##", "STATEMENT"))
                    else text[cut:].lstrip())
    # === THINK_FALLTHROUGH_FIX_V1: nothing matched. If reasoning markers are
    # present, the output is a failed generation (planning scratchpad with no
    # real content) — emit ONLY the warning, NEVER dump the scratchpad into the
    # body. If no reasoning markers, keep the text (legit content the anchors
    # didn't recognize). ===
    if has_reasoning:
        return ("[DRAFTER WARNING: section generation produced only reasoning, "
                "no motion content — REGENERATE THIS SECTION]")
    return text.lstrip()
    # === END THINK_FALLTHROUGH_FIX_V1 ===

# === IFTHEN_MERGE_V1 (additive; Path-A scorecard module, V4) ================
# Uses pipeline/scorecard_if_then.py (Path-A owned; lifted from the retiring
# reply_brief_drafter). Produces Section II (named defects) + Section III
# (IF/AND/THEN ending in void/voidable/stricken) from the detected violations
# in output/audit/data_<slug>.json.

def _stub_scope(stub_text):
    """Parse the stub's frontmatter scope + motion_type. Returns (scope, mtype)
    lowercased, or ('','')."""
    scope, mtype = "", ""
    for line in (stub_text or "").splitlines()[:30]:
        ls = line.strip().lower()
        if ls.startswith("scope:"):
            scope = ls.split(":", 1)[1].strip()
        elif ls.startswith("motion_type:"):
            mtype = ls.split(":", 1)[1].strip()
    return scope, mtype


# Which scorecard reasoning categories are RELEVANT to a given motion type.
# A 54b certification or interlocutory motion does NOT use the service/mailbox
# or merits-defect stacks; a vacate/service motion does. Agnostic by keyword.
def _ifthen_relevant(mtype, motion_title):
    blob = (str(mtype) + " " + str(motion_title)).lower()
    # appellate-vehicle motions: certification/interlocutory/mandamus — these
    # argue finality/jurisdiction, NOT record defects; suppress the defect stack.
    if any(k in blob for k in ("54(b)", "54b", "certif", "1292", "interlocutory",
                                "mandamus", "appeal")):
        return False
    return True


# === BEEZLE_PATCH_P2_MOTION_DEFECTS_V2 ===
# Replaces the original _ifthen_block_for with a version that lists every
# defect whose category supports THIS motion's posture (no cap), plus a
# reference to the Scorecard Exhibits A and B which hold the full case ledger.
#
# Original behavior is preserved as _ifthen_block_for_full for any caller
# that wants the wall-style Section II + III + governing-rules output.

def _ifthen_block_for_full(stub_text, motion_title, case_id=""):
    """Original behavior — inlines full Section II/III/governing-rules.
    Preserved for compatibility. Not used by default."""
    try:
        from pipeline.scorecard_if_then import (
            gather_scorecard_facts, build_section_ii, build_section_iii,
            _build_reasoning_stack,
        )
    except Exception as _e:
        print(f"  [ifthen-full] scorecard_if_then import failed ({_e})")
        return ""
    try:
        canonical = case_id or ""
        if not canonical:
            return ""
        _scope, _mtype = _stub_scope(stub_text)
        if not _ifthen_relevant(_mtype, motion_title):
            return ""
        facts = gather_scorecard_facts(canonical)
        if not facts:
            return ""
        is_state = canonical.startswith("phila_ccp") or ":ccp" in canonical.lower()
        sec_ii  = build_section_ii(facts, is_state) or []
        sec_iii = build_section_iii(facts, is_state) or []
        try:
            sec_stack = _build_reasoning_stack(facts, is_state, motion_title=motion_title) or []
        except Exception:
            sec_stack = []
        out = []
        if sec_ii:
            out.append("### DEFECTS IN THE RECORD")
            out.append("")
            out.extend(sec_ii)
        if sec_iii:
            out.append("### DETERMINISTIC RULE APPLICATION (IF / AND / THEN)")
            out.append("")
            out.extend(sec_iii)
        if sec_stack:
            out.append("")
            out.append("### GOVERNING RULES, AUTHORITY, AND EQUITY")
            out.append("")
            out.extend(sec_stack)
        return "\n".join(x for x in out if x is not None).strip()
    except Exception as _e:
        print(f"  [ifthen-full] skipped ({_e})")
        return ""


def _ifthen_block_for(stub_text, motion_title, case_id=""):
    """List every defect from the case scorecard whose category supports this
    motion's posture. No cap. Followed by a reference to the Scorecard
    Exhibits A and B which carry the full case ledger."""
    try:
        from pipeline.scorecard_if_then import (
            gather_motion_specific_facts, build_section_ii,
        )
    except Exception as _e:
        print(f"  [ifthen] scorecard_if_then import failed ({_e})")
        return ""
    try:
        canonical = case_id or ""
        if not canonical:
            return ""
        _scope, _mtype = _stub_scope(stub_text)
        if not _ifthen_relevant(_mtype, motion_title):
            return ""
        # max_facts=None  ->  list ALL motion-supporting defects
        facts = gather_motion_specific_facts(
            canonical, motion_title=motion_title, max_facts=None
        )
        if not facts:
            return ""
        is_state = canonical.startswith("phila_ccp") or ":ccp" in canonical.lower()
        sec_ii = build_section_ii(facts, is_state) or []
        out = []
        if sec_ii:
            out.append("### DEFECTS IN THE RECORD SUPPORTING THIS MOTION")
            out.append("")
            out.append("*The following defects from the case record support the "
                       "relief sought in this motion. The complete ledger of all "
                       "defects identified in this case is set forth in the "
                       "accompanying Scorecard Exhibits A and B.*")
            out.append("")
            out.extend(sec_ii)
            out.append("")
            out.append("See accompanying Scorecard Exhibits A and B for the full "
                       "case ledger of defects, authority violations, and rule "
                       "applications.")
        return "\n".join(x for x in out if x is not None).strip()
    except Exception as _e:
        print(f"  [ifthen] skipped ({_e})")
        return ""
# === END BEEZLE_PATCH_P2_MOTION_DEFECTS_V2 ===
# === SECTION_BY_SECTION_V1 ===
# Each defendant is an alias GROUP; matching any alias scopes the whole group.
_DEFENDANT_ALIASES = [
    ["transunion", "trans union"],
    ["experian"],
    ["equifax"],
    ["greensky", "green sky"],
    ["capital one", "capital_one", "capitalone"],
    ["avant"],
    ["amerisave"],
    ["state farm", "statefarm"],
    ["progressive"],
    ["lippi"],
]

def _scope_defendants(scope_text):
    """From a stub scope like 'Example Card Issuer only', return (in_scope_aliases,
    other_aliases). All aliases of a scoped defendant are in_scope; only
    aliases of DIFFERENT defendants are 'others'."""
    s = (scope_text or "").lower()
    in_scope, others = [], []
    for group in _DEFENDANT_ALIASES:
        if any(a in s for a in group):
            in_scope.extend(group)
        else:
            others.extend(group)
    return in_scope, others

# BEEZLE_FN_UNUSED 2026-06-13: _load_plaintiff_record (no static refs in pipeline/)
def _load_plaintiff_record(case_id, cap=14000, scope_text=""):
    """GROUNDING_V1: load the user's own filing record (plaintiff_facts) from the
    audit so the LLM knows what Plaintiff filed/served/said. Context only — never
    violations. Returns a formatted block or ''."""
    try:
        if not case_id:
            return ""
        import json as _json
        safe = case_id.replace(":", "_").replace("/", "_")
        ap = REPO / "output" / "audit" / f"data_{safe}.json"
        if not ap.exists():
            # try the fed_<n>_... vs fed_2_25 forms
            alt = REPO / "output" / "audit" / f"data_fed_2_{case_id.split(':')[-1]}.json"
            ap = alt if alt.exists() else ap
        if not ap.exists():
            return ""
        ad = _json.loads(ap.read_text(encoding="utf-8", errors="ignore"))
        pf = ad.get("plaintiff_facts", []) or []
        if not pf:
            return ""
        # SCOPE_PLAINTIFF_RECORD_V1: when the motion is scoped to a specific
        # defendant, drop facts that name a DIFFERENT defendant (prevents e.g.
        # Avant default facts entering a Example Card Issuer 54b motion). Case-general
        # facts (naming no other defendant) are kept.
        _in_scope, _others = _scope_defendants(scope_text)
        lines, used = [], 0
        for f in pf:
            t = (f.get("text", "") or "").strip()
            if not t:
                continue
            if _in_scope:
                _tl = t.lower()
                _sd_l = str(f.get("source_doc", "")).lower()
                _blob = _tl + " " + _sd_l
                names_other = any(o in _blob for o in _others if o not in _in_scope)
                names_scoped = any(i in _blob for i in _in_scope)
                # drop only if it names ANOTHER defendant and NOT the scoped one
                if names_other and not names_scoped:
                    continue
            sd = f.get("source_doc", "")
            entry = f"- {t}" + (f"  [{sd}]" if sd else "")
            if used + len(entry) > cap:
                break
            lines.append(entry); used += len(entry)
        if not lines:
            return ""
        return ("\n\n---PLAINTIFF'S RECORD (grounding; what Plaintiff has filed, "
                "served, and stated of record — treat as established, do not "
                "understate or omit)---\n" + "\n".join(lines) +
                "\n---END PLAINTIFF'S RECORD---\n")
    except Exception:
        return ""


def _scrub_grounding_leaks(text):
    """Strip leaked GROUNDING-SCAFFOLD sections the LLM copied verbatim from the prompt into the filing —
    render_packet's "Scanner-corroborated facts (use for fact section)", "Drafter checklist", and any heading
    that carries drafter guidance like "(use for ... section)". Removes from the offending heading down to the
    next same-or-higher-level heading (or a '---' rule / APPENDIX). Never raises; returns cleaned text."""
    if not text:
        return text
    import re as _re
    LEAK_HEAD = _re.compile(
        r"^(#{1,4})\s+.*(?:\(use for\b|scanner[- ]corroborated facts|drafter checklist|"
        r"grounding block|for drafter reference|do not (?:copy|paste)|use for fact section).*$", _re.I)
    lines = text.split("\n")
    out, i, n_drop = [], 0, 0
    while i < len(lines):
        m = LEAK_HEAD.match(lines[i].lstrip())
        if lines[i].lstrip().startswith("# APPENDIX"):
            out.extend(lines[i:]); break
        if m:
            level = len(m.group(1))
            i += 1
            # skip until next heading of same-or-higher level, or a horizontal rule
            while i < len(lines):
                s = lines[i].lstrip()
                hm = _re.match(r"^(#{1,4})\s", s)
                if (hm and len(hm.group(1)) <= level) or s.strip() == "---" or s.startswith("# APPENDIX"):
                    break
                i += 1
            n_drop += 1
            continue
        out.append(lines[i]); i += 1
    return "\n".join(out)


def _scrub_filename_leaks(text):
    """SCRUB_FILENAMES_V1 2026-06-09: Remove file/path references from LLM
    output. Catches scanner-block "**Source:** `...`" markdown bullet
    residue, backtick-quoted filenames, bare filename references in prose,
    and parenthetical file mentions. Prevents schema validator
    no_filename_leak failures.

    Safe: only matches strings that end with a true file extension
    (.docx, .doc, .pdf, .md, .txt), so legal abbreviations like
    "Pa.R.C.P." or "Fed. R. Civ. P." are NOT touched.
    """
    if not text:
        return text
    import re as _re

    # 1. Strip whole "**Source:** ..." markdown bullet lines (scanner residue)
    text = _re.sub(
        r"^\s*[-*]?\s*\*\*Source:\*\*\s*[^\n]*$",
        "",
        text,
        flags=_re.MULTILINE,
    )

    # 2. Strip backtick-quoted filenames: `anything.docx`, `anything.pdf`, etc.
    text = _re.sub(
        r"`[^`\n]*\.(?:docx?|pdf|md|txt)`",
        "[record]",
        text,
        flags=_re.IGNORECASE,
    )

    # 3. Strip bare filename references in prose: memorandum.docx, ex.pdf, etc.
    #    Only matches strings ending with a true file extension.
    text = _re.sub(
        r"\b\S+?\.(?:docx?|pdf|md)(?:\.ocr\.txt|\.txt)?\b",
        "[record]",
        text,
        flags=_re.IGNORECASE,
    )

    # 4. Strip parenthetical filename references: (filename: foo.docx), etc.
    text = _re.sub(
        r"\(\s*(?:[Ff]ilename|[Ss]ource|[Ff]ile)\s*:\s*[^)]+\)",
        "",
        text,
    )

    # 5. Cleanup: empty bullet artifacts, runs of 3+ blank lines
    text = _re.sub(r"^\s*[-*]\s*$\n?", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"\n{3,}", "\n\n", text)

    return text


def _groundize_sources(stub_text, case_id):
    """ECF_RESOLVER_V1 (2026-06-16): rewrite the stub's '**Source:** `filename`'
    lines to the real docket number ('**Source:** ECF No. N') so the LLM cites
    the correct number and never sees a raw filename to leak. Unresolvable docs
    become '**Source:** the record' -- NEVER a fabricated number (R7). Fixes the
    all-(ECF No. 130) guessing and the 'See <filename>' citation leaks."""
    if not stub_text or not case_id:
        return stub_text
    try:
        from pipeline.ecf_resolver import build_ecf_map, resolve
        emap = build_ecf_map(case_id)
    except Exception:
        return stub_text
    def _sub(m):
        n = resolve(m.group(1), emap)
        return f"**Source:** ECF No. {n}" if n else "**Source:** the record"
    return re.sub(r'\*\*Source:\*\*\s*`([^`]+)`', _sub, stub_text)


def _extract_active_defendant(stub_text):
    """SCOPE_CLAMP_V1: Extract the active defendant named in the stub's
    metadata block (line of form '**Defendant:** <name> (active)').
    Returns the defendant name or None if not present."""
    if not stub_text:
        return None
    _m = re.search(r"\*\*Defendant:\*\*\s*(.+?)\s*\(active\)", stub_text)
    if _m:
        return _m.group(1).strip()
    return None


def _section_prompt(stub_text, motion_filename, section):
    """Ask the LLM for ONE section only, with the full stub as context."""
    common = (
        f"Below is a motion stub for `{motion_filename}` containing all "
        f"controlling authority, opposing authority defects, and the "
        f"established record. Use facts ONLY as material to THIS motion.\n\n"
        f"INPUT STUB:\n---BEGIN STUB---\n{stub_text}\n---END STUB---\n\n"
    )
    # ECF_RESOLVER_V1: facts carry their real docket number in the Source line.
    common += (
        "CITATION RULE (mandatory): cite each fact by the 'ECF No. N' shown in "
        "its Source. If a Source says 'the record', cite it as '(record)'. NEVER "
        "invent or guess an ECF number, and NEVER output a file name.\n\n"
    )
    # === SCOPE_CLAMP_V1 2026-06-09 (Batch 4 gated 2026-06-13) ===
    # Inject a defendant-scope clamp pulled from the stub's
    # "**Defendant:** <name> (active)" metadata line. Prevents the LLM
    # from enumerating co-defendants or underlying-action defendants as
    # separate Roman-numeral argument sections.
    # GATE: case-level filings (omnibus, remand, 60(b), appellate, etc.)
    # MUST address all parties under one consolidated argument structure;
    # the per-defendant clamp would contradict CASE_LEVEL_FIX_V1, so we
    # suppress it here for those types.
    _active_def = (None if _is_case_level_filing(motion_filename)
                   else _extract_active_defendant(stub_text))
    if _active_def:
        common += (
            f"DEFENDANT SCOPE CLAMP (mandatory):\n"
            f"This filing addresses ONLY the motion filed by **{_active_def}**. "
            f"You MUST NOT enumerate or address other parties as separate "
            f"argument sections or as separate fact-introduction paragraphs. "
            f"The CASE AUDIT may list co-defendants in this action and the "
            f"UNDERLYING-ACTION defendants (named in CASE_FACTS' \"Defendants "
            f"in underlying\" line, e.g. parties to a separate state-court "
            f"action that produced an underlying judgment) -- those parties "
            f"are CONTEXT for the bad-faith / breach / coverage theory, NOT "
            f"adversaries in THIS filing. Each Roman numeral in the ARGUMENT "
            f"section is a distinct LEGAL GROUND (a claim or theory), NEVER "
            f"a different defendant. If a co-defendant or underlying-action "
            f"party becomes relevant to a legal ground, mention them WITHIN "
            f"that ground; do NOT create a new Roman numeral for each party. "
            f"Do NOT generate one argument section per party listed in "
            f"CASE_FACTS.\n\n"
        )
    # === END SCOPE_CLAMP_V1 ===
    if section == "introduction":
        return common + (
            "Write ONLY the INTRODUCTION. Output exactly:\n\n"
            "## INTRODUCTION\n\n"
            "[Write the INTRODUCTION as ONE continuous paragraph of prose, "
            "3-6 sentences total. DO NOT use numbered paragraphs (no '1.', "
            "'2.', '3.'). DO NOT use bullets or sub-headings. DO NOT "
            "enumerate sub-clauses as a list. The paragraph must read as "
            "ordinary motion prose and weave together, in sentence form: "
            "what this motion asks the Court to do; what Plaintiff has "
            "already filed or done of record that supports it (draw on "
            "PLAINTIFF'S RECORD grounding below; cite the filing/date/"
            "tracking where relevant); and the current procedural posture. "
            "Be precise and do NOT understate what Plaintiff filed. No "
            "argument, no relief list.]\n\n"
            "Output ONLY this section, as a SINGLE prose paragraph. Do not "
            "write STATEMENT OF FACTS, ARGUMENT, or PRAYER."
        )
    if section == "facts":
        return common + (
            "Write ONLY the STATEMENT OF FACTS section. Output exactly:\n\n"
            "## STATEMENT OF FACTS\n\n"
            "[Concise numbered paragraphs stating ONLY the facts material to "
            "the relief THIS motion seeks. Draw from the CASE AUDIT and the stub, "
            "in your OWN words — do NOT reproduce every scanner entry, do NOT quote "
            "raw document text, do NOT recite the docket activity log. Cite court "
            "filings by docket number as 'ECF No. N' (use the DOCKET HIGHLIGHTS "
            "table); NEVER cite a filename or .pdf/.docx. EXCLUDE any fact about a "
            "party or matter not at issue in this motion, and any non-record or "
            "stray-document content. Do NOT repeat any fact or pad; if the "
            "record supports few facts, write few paragraphs (3-8 is normal). "
            "Mark genuine gaps "
            "'[FACT NOT IN RECORD - DRAFTER MUST VERIFY]'.]\n\n"
            "Output ONLY this section. Do not write ARGUMENT or PRAYER."
        )
    # ===== APPELLATE SECTIONS (Pa.R.A.P. 2111 / FRAP 28) — error-correction, NOT relitigation =====
    if section == "app_case":
        return common + (
            "You are drafting an APPELLATE BRIEF for an appellate court — this is ERROR-CORRECTION review "
            "of the trial court's orders, NOT a re-trial. Write ONLY the STATEMENT OF THE CASE "
            "(Pa.R.A.P. 2117 / FRAP 28(a)(6)). Output exactly:\n\n"
            "## STATEMENT OF THE CASE\n\n"
            "[A concise procedural history in numbered paragraphs: the form of the action, the material "
            "procedural events IN ORDER (from the CASE AUDIT / docket / timeline), the orders in question "
            "with their entry dates and Control numbers, and the disposition below. State only what the "
            "record shows; cite each fact to the record ('R.__a') or the docket entry. Do NOT argue. Do "
            "NOT re-plead the merits. Do NOT invent facts; if a needed fact is not in the record, omit it. "
            "No standard-of-review discussion here.]\n\n"
            "Output ONLY this section."
        )
    if section == "app_summary":
        return common + (
            "Write ONLY the SUMMARY OF ARGUMENT (Pa.R.A.P. 2118 / FRAP 28(a)(7)) for this APPELLATE brief. "
            "Output exactly:\n\n## SUMMARY OF ARGUMENT\n\n"
            "[A succinct, accurate condensation of the argument — ONE short paragraph per Question presented, "
            "in the same order as the Statement of the Questions Involved. State, for each, the error assigned, "
            "the standard of review, and why reversal/vacatur follows. This is a summary: NO citations, NO "
            "block quotes, do NOT repeat the argument in full.]\n\n"
            "Output ONLY this section."
        )
    if section == "app_argument":
        try:
            _cg = get_canon_grounding(stub_text) or ""
        except Exception:
            _cg = ""
        try:
            _rebut = get_pseudolaw_rebuttal_grounding(stub_text) or ""
        except Exception:
            _rebut = ""
        _extra = ((_rebut.rstrip() + "\n\n") if _rebut else "") + ((_cg + "\n") if _cg else "")
        # JURISDICTION-AWARE citation directive: a PA Superior Court appeal cites PENNSYLVANIA authority;
        # a federal Court of Appeals (Third Circuit) appeal cites FEDERAL authority.
        _appct = _appellate_court_for(motion_filename)
        if _appct == '3D_CIR':
            _cite_rule = (
                "For this FEDERAL appeal to the Third Circuit, cite FEDERAL authority: the Federal Rules of "
                "Civil/Appellate Procedure, the governing federal statutes (e.g. FCRA, 15 U.S.C. § 1681 et "
                "seq.), Supreme Court and Third Circuit precedent, and Fed. R. Civ. P. 60(b) where a judgment "
                "is challenged as void. State the standard of review the Third Circuit actually applies "
                "(de novo for dismissals/questions of law; abuse of discretion for discretionary rulings).")
        else:
            _cite_rule = (
                "For this PENNSYLVANIA (Superior Court) appeal cite PENNSYLVANIA authority (Pa.R.C.P., "
                "Pa.R.A.P., 42 Pa.C.S., Pa. Const.) — do NOT cite the Federal Rules of Civil Procedure or "
                "Rule 60(b) as the governing rule; the PA analog to void-judgment relief is 42 Pa.C.S. § 5505 "
                "and the common-law void-order doctrine.")
        return common + _extra + (
            "Write ONLY the ARGUMENT (Pa.R.A.P. 2119 / FRAP 28(a)(8)) for this APPELLATE brief. This is an "
            "APPEAL: you are showing the appellate court that the TRIAL COURT ERRED. Do NOT relitigate or "
            "re-plead the trial motions; assign error to the orders below.\n\n"
            "MANDATORY structure — divide the Argument into as many parts as there are Questions Involved, "
            "in the SAME ORDER, each headed by the point:\n\n"
            "## ARGUMENT\n\n"
            "### I. [The point of Question 1, in a distinctive heading]\n\n"
            "*Standard of review:* [state it — de novo/plenary for questions of law and voidness; abuse of "
            "discretion for discretionary rulings]. *Preservation:* [where the issue was raised below "
            "(motion + date), or that it is a non-waivable voidness/jurisdiction challenge (Pa.R.A.P. 302)].\n\n"
            "[Argument prose: identify the specific ORDER (Control number + date) and the MANDATORY rule or "
            "duty it violated; apply the controlling authority to the record; cite the record as 'R.__a' and "
            "the Scorecard as filed of record. Use the controlling LEGAL meaning of any term of art "
            "(possession/property/allege/claim/misnomer) per the definition grounding — do NOT concede the "
            "common meaning. Cite ONLY authority in the stub/grounding/record; never invent a citation.]\n\n"
            "### II. [The point of Question 2] ... and so on for each Question.\n\n"
            "RULES: one Argument part per Question; head each with the point + standard of review + preservation. "
            + _cite_rule +
            " No numbered flat paragraphs; no Proposed Order; no Prayer.\n\n"
            "Output ONLY the ARGUMENT section."
        )
    if section == "argument":
        # COMPREHENSION-DRIVEN canons: pull only the canons that bear on THIS stub's
        # statutes/claims from m0's grounded notes; fall back to a lean comprehension
        # nudge (NOT a fixed recitation) when nothing specific is retrieved.
        try:
            _cg = get_canon_grounding(stub_text) or ""
        except Exception:
            _cg = ""
        _canon_block = (_cg + "\n") if _cg else (
            "INTERPRETIVE CANONS (comprehension, not a checklist): where a governing "
            "statute, rule, or contract term in the record is contested, apply and NAME "
            "the canon your reading actually supports -- a remedial consumer statute "
            "(FCRA/FDCPA/TILA/RESPA/UTPCPL) construed liberally for the plaintiff; "
            "ambiguity in the defendant's own drafted contract against the drafter "
            "(contra proferentem); 'shall' as a mandatory duty; expressio unius; or de "
            "novo reading with no agency deference (Loper Bright, 603 U.S. 369 (2024)). "
            "Mention a canon ONLY where it genuinely governs the text; none if none does. "
            "Grounded UCC/capacity/redemption/discharge/securitization arguments are "
            "permitted where the corpus supports them; only court-sanctioned sovereign-"
            "citizen schemes are barred (system rule 4).\n\n")
        # Non-conceding dismissal posture: never adopt the court's dismissal as fact; m0
        # finds the reversal/restoration authority (graph + corpus) to restore each claim.
        try:
            _restore = get_restoration_grounding(stub_text) or ""
        except Exception:
            _restore = ""
        if _restore:
            _canon_block = _restore.rstrip() + "\n\n" + _canon_block
        # If opposing counsel cried "pseudolaw/frivolous", rebut with full-corpus backing.
        try:
            _rebut = get_pseudolaw_rebuttal_grounding(stub_text) or ""
        except Exception:
            _rebut = ""
        if _rebut:
            _canon_block = _rebut.rstrip() + "\n\n" + _canon_block
        return common + (
            "GROUNDING RULES (mandatory): Cite court filings ONLY by docket "
            "number as 'ECF No. N' (use the DOCKET HIGHLIGHTS table where "
            "present); NEVER cite a filename or .pdf/.docx path. Do NOT paste "
            "raw quoted text from scanner entries, the docket activity log, "
            "or opposing filings; paraphrase any source material in your own "
            "words and cite the docket entry. EXCLUDE any matter that does "
            "not relate to THIS motion.\n\n"
            "Write ONLY the ARGUMENT section using this MANDATORY hierarchical "
            "structure (do NOT write flat numbered paragraphs):\n\n"
            "## ARGUMENT\n\n"
            "### I. [First ground - a one-line thesis tied to the motion type]\n\n"
            "A. [A paragraph applying a controlling authority to the facts. Cite "
            "authorities by their exact citation strings from the stub.]\n\n"
            "B. [A second sub-point paragraph. Where opposing authority is flagged "
            "defective, name the defect and rebut it here.]\n\n"
            "### II. [Second ground if the stub supports one]\n\n"
            "A. [...]\n\n"
            "B. [...]\n\n"
            "RULES: Each Roman numeral is a distinct legal ground. Each A./B./C. "
            "sub-point is one paragraph of argument prose (NOT a numbered list, NOT "
            "'First/Second/Third'). Use capital-letter sub-points A., B., C. only.\n\n"
            "SERVICE/NOTICE REASONING (apply ONLY if the record or a challenged "
            "order involves service of process, notice, or mailing; invoke ONLY the "
            "strands the record supports; never invent a mailing, address, date, or "
            "service event): When a party or order asserts that a defendant was not "
            "served or improperly served, and the record shows service was made, "
            "construct the rebuttal with PRECISION:\n"
            "  (1) Name the OPERATIVE Federal Rule of Civil Procedure 4 subsection "
            "for the served party's type -- for a corporation, LLC, partnership, or "
            "association: Rule 4(h)(1)(A) (incorporating Rule 4(e)(1) state-law "
            "methods, which in Pennsylvania permit service by certified mail) or "
            "Rule 4(h)(1)(B) (delivery to an officer or managing/general agent); for "
            "an individual: Rule 4(e)(1) or 4(e)(2). Do NOT cite bare 'Rule 4' -- "
            "always the subsection.\n"
            "  (2) Proof of service is governed by Rule 4(l)(1) (server's affidavit/"
            "return). Critically, Rule 4(l)(3) provides that 'Failure to prove "
            "service does not affect the validity of service' -- so any defect in "
            "the proof does not undo service actually made.\n"
            "  (3) Where the record shows a properly addressed mailing (e.g., "
            "certified mail/USPS tracking to the address of record) that was not "
            "returned, and subsequent mailings reached the same address, invoke the "
            "rebuttable PRESUMPTION OF RECEIPT: Rosenthal v. Walker, 111 U.S. 185, "
            "193 (1884) (a letter properly directed and placed in the mail is "
            "presumed to have reached its destination and been received); accord "
            "Rios v. Nicholson, 490 F.3d 928, 930-31 (Fed. Cir. 2007). The burden "
            "then shifts to the party denying receipt.\n"
            "  (4) Ground that presumption in the PRESUMPTION OF REGULARITY -- "
            "official duties, including those of postal employees, are presumed to "
            "have been performed properly (omnia praesumuntur rite esse acta).\n"
            "  (5) If the action was REMOVED, note that under 28 U.S.C. Sec 1448 the "
            "removing party, having invoked federal jurisdiction, bears the burden "
            "to complete any service left incomplete; its own failure (or its "
            "counsel's) cannot be charged against the opposing party.\n"
            "  (6) Where the opposing party slept on a service objection while the "
            "case proceeded, note that equity aids the vigilant, not those who "
            "slumber on their rights (vigilantibus non dormientibus aequitas "
            "subvenit).\n\n"
            + _canon_block +
            "Output ONLY this section. Do not repeat the facts or write PRAYER."
        )
    if section == "prayer":
        return common + (
            "Write ONLY the PRAYER FOR RELIEF section. Output exactly:\n\n"
            "## PRAYER FOR RELIEF\n\n"
            "WHEREFORE, Plaintiff respectfully requests that this Court:\n"
            "1. [Specific relief tied to the motion type and the stub's remedies]\n"
            "2. [Additional relief if supported]\n"
            "3. Grant such other and further relief as this Court deems just and proper.\n\n"
            "Output ONLY this section."
        )
    # v6: Black's-anchored grounding injection (drafter_grounding)
    _grounding = get_section_grounding(stub_text, top_k=8)   # broadened: surface case-holding text, not memory-cites
    if _grounding:
        common = _grounding + "\n\n" + common

    return common


def _load_case_audit(case_id):
    """SUBSTANCE_V2: ground the draft in the case's own CASE_FACTS.md audit
    (postures, per-defendant violations, latest court order, service issue,
    strategy, docket) instead of the raw plaintiff_facts Gmail dump. Agnostic:
    any case with a CASE_FACTS.md works. Returns a grounding block or ''."""
    try:
        if not case_id:
            return ""
        cf = REPO / "obsidian_sync" / "cases" / case_id_to_folder(case_id) / "CASE_FACTS.md"
        if not cf.exists():
            return ""
        txt = cf.read_text(encoding="utf-8", errors="ignore").strip()
        if not txt:
            return ""
        return ("\n\n---CASE AUDIT (the established record for this case: "
                "defendant postures, per-defendant violations, the latest court "
                "order and its defects, service facts, docket, and strategy. "
                "This is the factual and procedural basis for the filing. Cite "
                "docket entries as 'ECF No. N' using the DOCKET HIGHLIGHTS table "
                "where present. Do NOT cite raw filenames. Do NOT dump the docket "
                "activity log; use only what is material to THIS motion.)---\n"
                + txt +
                "\n---END CASE AUDIT---\n")
    except Exception:
        return ""

def _dedup_paragraphs(text):
    """DEDUP_V1: collapse runaway-repeated numbered paragraphs (LLM degeneration
    on thin sections produced e.g. 79 identical mandamus paras). Keeps the first
    occurrence of each fact, drops exact + near-exact repeats, renumbers 1..N.
    Agnostic; never alters content, only removes duplicate numbered paragraphs."""
    import re as _re
    if not text:
        return text
    lines = text.split("\n")
    out, seen, n = [], [], 0
    def _norm(x):
        x = _re.sub(r'^\s*\d+\.\s*', '', x)
        x = _re.sub(r'\s+', ' ', x).strip().lower()
        return x
    for ln in lines:
        if _re.match(r'^\s*\d+\.\s+', ln):
            key = _norm(ln)
            if not key:
                continue
            dup = any(key == sx or (len(sx) > 40 and key.startswith(sx[:60])) for sx in seen)
            if dup:
                continue
            seen.append(key)
            n += 1
            out.append(_re.sub(r'^\s*\d+\.', f"{n}.", ln, count=1))
        else:
            out.append(ln)
    return "\n".join(out)

def _dedup_top_level_sections(text):
    """DEDUP_SECTIONS_V1 (2026-07-12): drop a REPEATED top-level '## SECTION' block.
    A section re-roll (e.g. the argument section regenerated at higher temp) can emit the
    whole '## ARGUMENT ... ' block twice with only slightly different sub-headings, which
    _dedup_paragraphs (paragraph-level) and the docx page-break-section dedup (page-break
    sections only) both miss. This keeps the FIRST occurrence of each normalized top-level
    heading and removes any later duplicate together with its body (up to the next top-level
    '## ' heading). Only two-hash '## ' headings are considered; '### ' sub-headings and all
    body prose are untouched. Conservative: top-level sections are unique by design."""
    import re as _re
    if not text:
        return text
    out, seen, skipping = [], set(), False
    for ln in text.split("\n"):
        m = _re.match(r'^##\s+(\S.*?)\s*$', ln)      # exactly two '#', not '### '
        if m:
            key = _re.sub(r'[^a-z0-9 ]', '', m.group(1).lower())
            key = _re.sub(r'\s+', ' ', key).strip()
            if key and key in seen:                  # duplicate top-level section -> skip it + its body
                skipping = True
                continue
            seen.add(key)
            skipping = False
            out.append(ln)
            continue
        if skipping:                                 # inside a dropped duplicate section
            continue
        out.append(ln)
    return "\n".join(out)

_ARG_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV",
              "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII", "XXIII", "XXIV"]

# === VEHICLE-AWARE GROUND FILTER (2026-08-01) — grounded in the Rule 60(b)(4) void-judgment doctrine ===
# Obsidian [[frcp-rule-60-b-4-void-judgment-doctrine]] (corpus-grounded): a judgment is VOID — not merely
# voidable — ONLY IF the court lacked subject-matter jurisdiction, lacked personal jurisdiction, or violated
# due process. Legal error (a court that HAD jurisdiction but erred — Twombly misapplied, stare decisis
# ignored) is VOIDABLE and is corrected on APPEAL, not by a Rule 60(b)(4) motion. Voidness is a MANDATORY,
# non-discretionary ground. A Rule 60(b)(4) motion therefore argues VOIDNESS grounds ONLY; dumping merits/
# legal-error grounds into it dilutes the one non-discretionary lever and hands the court a foothold to deny.
# This is legal STRUCTURE (which defect goes to voidness), not case/judge data — R2-safe. A ground dropped
# here is NOT forfeited: it is argued in the appeal, its correct vehicle.
_VOIDNESS_GROUNDS = {
    "VOID_JUDGMENT_RULE_60B4",
    "SUBJECT_MATTER_JURISDICTION_DEFECT",
    "STANDING_DEFECT",                 # Article III standing IS subject-matter jurisdiction
    "REAL_PARTY_RECIPROCITY",          # enforcing filer never proved holder/PETE status => no standing => void (rides STANDING_DEFECT; Rule 17(a) + U.C.C. 3-301)
    "REMOVAL_DEFECTIVE",               # defective removal => no removal/subject-matter jurisdiction
    "PERSONAL_JURISDICTION_DEFECT",
    "SERVICE_DEFECTIVE",               # defective service => no personal jurisdiction => void (Espinosa)
    "PROCEDURAL_DUE_PROCESS_VIOLATION",
    "NOTICE_INADEQUATE",               # inadequate notice = due-process voidness (Mullane)
    "FRAUD_ON_THE_COURT",              # independent vacatur ground going to the integrity/due-process of the judgment
}


def _is_voidness_vehicle(motion_filename):
    """True iff the filing is a Rule 60(b)(4)/void-judgment vacatur — the vehicle whose ONLY cognizable
    grounds are voidness (lack of SMJ/PJ, or a due-process violation). Every other vehicle is unrestricted."""
    try:
        stem = _filing_type_from_stem(Path(str(motion_filename)).stem)
    except Exception:
        stem = str(motion_filename or "").lower()
    return any(k in stem for k in ("vacate", "60b4", "60(b)(4)", "void_judgment", "voidjudgment", "void_order"))


# An ACCOUNTABILITY filing (Rule 18 petition for review / JCB / ODC / judicial-conduct complaint) is about
# a DECISIONMAKER'S conduct — the mandatory-duty failures of the judge — NOT the opposing party's pleading
# defects. Its cognizable grounds are the judge-conduct set only; the litigation grounds (removal, service,
# verification, Twombly/Iqbal, standing) belong to the merits filings, not the conduct forum.
_JUDGE_CONDUCT_GROUNDS = {
    "PROCEDURAL_DUE_PROCESS_VIOLATION",
    "SUA_SPONTE_RULING",
    "NOTICE_INADEQUATE",
    "FRAUD_ON_THE_COURT",
    "JUDICIAL_DISQUALIFICATION_DUTY",
    "JUDICIAL_CANON_VIOLATION",
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW",
    "STARE_DECISIS_VIOLATION",
    "NON_BINDING_AS_CONTROLLING",
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY",
    "MANDATORY_JUDICIAL_NOTICE_IGNORED",
    "NO_REASONS_STATED",
    "BIAS_EMOTIONAL_LANGUAGE",
    "BIAS_PRO_SE_GATEKEEPING",
    "REAL_PARTY_RECIPROCITY",          # asymmetric application of the standing/real-party rule BY the decisionmaker = even-handedness/due-process duty failure
    "SOVEREIGN_CITIZEN_LABEL",
    "OBSTRUCTION_OF_JUSTICE",
    "JUDICIAL_OATH_BREACH",
    "JUDICIAL_CANON_BREACH",
    "record_contradiction",
}


def _is_accountability_vehicle(motion_filename):
    """True iff the filing is a judicial-conduct / attorney-discipline accountability filing (Rule 18
    petition for review, JCB complaint, ODC complaint, § 351 misconduct referral)."""
    try:
        stem = _filing_type_from_stem(Path(str(motion_filename)).stem)
    except Exception:
        stem = str(motion_filename or "").lower()
    stem = (stem or "").lower()
    return any(k in stem for k in ("rule18", "rule_18", "petition_for_review", "jcb_complaint",
                                   "odc_complaint", "judicial_council", "judicial_misconduct",
                                   "misconduct_complaint", "accountability"))


def _grounds_for_vehicle(grounds, motion_filename):
    """Vehicle-aware ground filter. A Rule 60(b)(4) motion argues VOIDNESS grounds ONLY (grounded in the
    void-judgment doctrine above). Dropping a non-voidness ground here does NOT forfeit it — it is argued in
    the appeal, its correct vehicle. Every other filing type is returned UNCHANGED. Never blanks the motion
    (falls back to all grounds if nothing matched). Never raises."""
    try:
        if _is_accountability_vehicle(motion_filename):
            kept_c = [g for g in grounds if (g.get("type") or "") in _JUDGE_CONDUCT_GROUNDS]
            dropped_c = sorted({(g.get("type") or "") for g in grounds
                                if (g.get("type") or "") not in _JUDGE_CONDUCT_GROUNDS})
            if kept_c and dropped_c:
                print(f"  [vehicle] accountability conduct filter: kept {len(kept_c)} judge-conduct ground(s), "
                      f"routed {len(dropped_c)} litigation ground(s) to the merits filings "
                      f"({', '.join(dropped_c[:6])})")
            return kept_c or grounds
        if not _is_voidness_vehicle(motion_filename):
            return grounds
        kept = [g for g in grounds if (g.get("type") or "") in _VOIDNESS_GROUNDS]
        dropped = [g.get("type") for g in grounds if (g.get("type") or "") not in _VOIDNESS_GROUNDS]
        if kept and dropped:
            print(f"  [vehicle] 60(b)(4) voidness filter: kept {len(kept)} voidness ground(s), routed "
                  f"{len(dropped)} legal-error/merits ground(s) to the appeal ({', '.join(dropped[:6])})")
        return kept or grounds
    except Exception:
        return grounds


def _argument_scaffold(grounds):
    """Explicit numbered ARGUMENT structure — one Part per detected ground, jurisdictional first — appended to
    the SINGLE argument-section call so it develops every ground (2-4 paragraphs each) instead of the LLM's
    default 3-issue consolidation. Pure string builder (no LLM calls); the one full-budget argument call fills
    the scaffold. Keeps the section-by-section design intact."""
    n = len(grounds)
    last = _ARG_ROMAN[n - 1] if n <= len(_ARG_ROMAN) else str(n)
    lines = [f"\n\n=== REQUIRED ARGUMENT STRUCTURE — you MUST output ALL {n} numbered Parts (I through {last}), "
             f"in this order, none merged, none skipped, none renumbered ===",
             f"Write your ## ARGUMENT as exactly these {n} Roman-numbered Parts, one per ground. Each Part is an "
             "INDEPENDENT ground for relief (2-4 paragraphs) — a Part omitted is a win forfeited, so skipping any "
             "number is an error. Lead with the jurisdictional Parts; they are non-waivable and dispositive. "
             "BUILD EACH PART ON ITS LADDER from the 'AUTHORITY STACK PER GROUND' block above, in descending "
             "order of force: (1) open with the controlling constitutional/jurisdictional anchor; (2) quote the "
             "statute or rule's operative 'shall/void/must' language verbatim; (3) STATE THE BINDING HOLDING — "
             "name the controlling case and say what it HELD (use the holding shown in the ladder; where the "
             "ladder gives only the case name, state the holding you can support from the record/grounding — "
             "never invent one); (4) apply it to THIS record; (5) close with the mandatory consequence (void / "
             "non-waivable / the court MUST vacate — discretion foreclosed). Cite ONLY the authority bound to "
             "this Part; never borrow another Part's citation. Naming a case without stating its holding is "
             "incomplete — the holding is what forecloses a narrow reading."]
    for i, g in enumerate(grounds):
        rn = _ARG_ROMAN[i] if i < len(_ARG_ROMAN) else str(i + 1)
        label = (g.get("type") or "").replace("_", " ").title()
        lines.append(f"{rn}. {label}  [authority for this Part, cite only this: {g.get('cite', '')}]")
    lines.append(f"Emit every Part I through {last}. Each Part's heading is '### <Roman>. <a legal thesis in a "
                 f"few words>' — the heading states the POINT (e.g. 'Plaintiff Has Article III Standing'); it "
                 f"must NEVER contain the citation, the bracketed authority note, or the words 'cite only'. Put "
                 f"the citation in the body. Do NOT consolidate, stop early, or skip a number — all {n} Parts "
                 f"must appear.")
    return "\n".join(lines)


def _fill_missing_grounds(argument_md, stub_text, motion_filename, grounds):
    """Guarantee EVERY detected ground appears as a Part. The one argument call occasionally skips a number
    (e.g. a §1983-against-a-judge ground the model balks at); this detects the gap by heading and fills ONLY the
    missing ground(s) with a targeted call each — the drafter's OWN call_llm/_section_prompt, a repair, not a
    parallel drafter. Fires for the gaps only (usually 0-1), so it never becomes the rejected per-ground fan-out."""
    import re as _re
    heads = "\n".join(_re.findall(r"^###\s+.*$", argument_md, _re.M)).lower()
    base = _section_prompt(stub_text, motion_filename, "argument")
    existing = len(_re.findall(r"^###\s+[IVXLC]+\.", argument_md, _re.M))
    added = 0
    for g in grounds:
        label = (g.get("type") or "").replace("_", " ").lower()
        words = [w for w in label.split() if len(w) > 3]
        if words and all(w in heads for w in words[:2]):      # ground already has a Part
            continue
        idx = existing + added
        rn = _ARG_ROMAN[idx] if idx < len(_ARG_ROMAN) else str(idx + 1)
        cite = g.get("cite") or ""
        focus = (
            f"\n\n=== Draft ONLY this ONE missing ARGUMENT Part (the prior draft skipped it) ===\n"
            f"Part {rn}: {label.title()} — cite ONLY {cite}\n"
            + (f"RULE TEXT: {g['rule_text'][:400]}\n" if g.get("rule_text") else "")
            + (f"WHY IT GOVERNS THIS RECORD: {g['explanation'][:300]}\n" if g.get("explanation") else "")
            + f"Write '### {rn}. <point>' then 2-3 paragraphs applying {cite} to THIS record, quoting the rule's "
              f"operative language. Cite ONLY {cite}. Filed prose only — no meta-commentary, no outline."
        )
        try:
            add = call_llm(base + focus, enable_thinking=False, max_tokens=1200)
            add = _clean_llm_output(add, section="argument") if add else ""
            add = _scrub_filename_leaks(_strip_reasoning_leak(add))
            if add.strip():
                argument_md = argument_md.rstrip() + "\n\n" + add.strip()
                added += 1
                print(f"  [argument] filled skipped ground -> Part {rn}. {label.title()}")
        except Exception as _fe:
            print(f"  [argument] could not fill {label} ({str(_fe)[:40]})")
    return argument_md


def _renumber_argument_parts(argument_md, grounds):
    """After the argument call + gap-fill, Parts can have GAP numbers (the LLM skipped III/IV), Arabic fill
    numbers appended at the end, and jurisdictional grounds out of order. Split into Parts, order them by the
    detected-grounds order (jurisdictional first), and renumber I..N sequentially. Deterministic; keeps the
    '## ARGUMENT' header and any trailing '### DEFECTS IN THE RECORD' block in place and last."""
    import re as _re
    segs = _re.split(r"(?m)^(?=###\s)", argument_md)
    head = segs[0]
    ground_blocks, tail_blocks = [], []
    for b in segs[1:]:
        if not b.strip():
            continue
        if "defects in the record" in b.splitlines()[0].lower():
            tail_blocks.append(b)
        else:
            ground_blocks.append(b)
    order_words = {}
    for i, g in enumerate(grounds):
        lbl = (g.get("type") or "").replace("_", " ").lower()
        order_words[i] = [w for w in lbl.split() if len(w) > 3][:2]

    def _rank(b):
        hl = b.splitlines()[0].lower()
        for i, words in order_words.items():
            if words and all(w in hl for w in words):
                return i
        return 999
    ground_blocks.sort(key=_rank)
    out_blocks = []
    for i, b in enumerate(ground_blocks):
        rn = _ARG_ROMAN[i] if i < len(_ARG_ROMAN) else str(i + 1)
        out_blocks.append(_re.sub(r"^###\s+[IVXLC0-9]+\.\s*", f"### {rn}. ", b, count=1).rstrip())
    return head.rstrip() + "\n\n" + "\n\n".join(out_blocks + [t.rstrip() for t in tail_blocks]) + "\n"


def _draft_sectioned(stub_text, motion_filename, case_id):
    """Draft FACTS, ARGUMENT, PRAYER as three separate full-budget calls and
    concatenate. Prevents a long facts section from starving the argument/prayer
    (the single-shot 8000-token cap truncated long motions like 60d3)."""
    # APPELLATE briefs are error-correction under a fixed rule structure (Pa.R.A.P. 2111 / FRAP 28),
    # NOT motion sections. Route them to the appellate assembler.
    if _is_appellate_filing(motion_filename):
        return _draft_appellate_sectioned(stub_text, motion_filename, case_id)
    parts = []
    # GROUNDING_V1: append the plaintiff record to the stub so every section
    # (esp. the introduction) is grounded in what Plaintiff actually filed.
    _pr = _load_case_audit(case_id)  # SUBSTANCE_V2: CASE_FACTS audit, not Gmail dump
    stub_text = (stub_text + _pr) if _pr else stub_text
    stub_text = _groundize_sources(stub_text, case_id)  # ECF_RESOLVER_V1: real ECF, no filename leak
    for sect in ("introduction", "facts", "argument", "prayer"):
        if sect == "argument":
            # COMPLETE ARGUMENT (2026-07-12): the argument section already gets its OWN full-budget call
            # (that's what section-by-section is for). It was only using ~4.6k of 8k tokens on a default 3-issue
            # argument, leaving standing / subject-matter jurisdiction / sanctions / §1983 on the table. Append an
            # ENUMERATED SCAFFOLD — one numbered Part per detected ground, jurisdictional first — so the SINGLE
            # call develops every ground, and raise the budget to fit them. No extra calls; the design is intact.
            try:
                from pipeline.drafter_grounding import detected_grounds as _dg_grounds
                _grounds = _dg_grounds(stub_text)
                _grounds = _grounds_for_vehicle(_grounds, motion_filename)  # 60(b)(4)=voidness grounds only
            except Exception:
                _grounds = []
            if _grounds:
                print(f"  [argument] enumerated {len(_grounds)} grounds into the single argument call (jurisdictional first)")
                prompt = _section_prompt(stub_text, motion_filename, sect) + _argument_scaffold(_grounds)
                # budget scales with ground count (~700 tok/ground) so a long list of Parts can't be truncated;
                # capped at 20k. Every detected ground gets developed — none dropped for lack of room.
                _arg_budget = min(20000, max(12000, 900 * len(_grounds)))
                out = call_llm(prompt, enable_thinking=False, max_tokens=_arg_budget)
                out = _clean_llm_output(out, section=sect) if out else ""
                out = _scrub_filename_leaks(out)
                out = _dedup_paragraphs(out)
                out, _ = _dedup_repeated_paragraphs(out)
                out, _ = _dedup_repeated_sentences(out)   # L8-DEDUP on the ARGUMENT path too: kills sentence-level
                                                          # degeneration loops (e.g. a service point repeated 4x) that
                                                          # the paragraph-dedup misses — the source of the 10k-word bloat
                if out.strip():
                    out = _fill_missing_grounds(out, stub_text, motion_filename, _grounds)   # 100% coverage repair
                    out = _renumber_argument_parts(out, _grounds)                            # clean I..N, jurisdictional first
                    out, _ = _dedup_repeated_sentences(out)   # scrub the fill-repair output as well
                    parts.append(out.strip())
                continue
        prompt = _section_prompt(stub_text, motion_filename, sect)
        # enable_thinking=False forces the answer into `content` (no separate reasoning stream), so a section
        # can't come back empty-content and leak its prompt scaffold/[thesis] placeholders — the same fix the
        # appellate path (_llm_argument_part) and m0_*_gate already use. Prevents the "produced only reasoning"
        # DRAFTER WARNING + template leak seen on complex sections (e.g. the IFP argument).
        out = call_llm(prompt, enable_thinking=False)
        out = _clean_llm_output(out, section=sect) if out else ""
        out = _scrub_filename_leaks(out)  # SCRUB_FILENAMES_V1 2026-06-09
        out = _dedup_paragraphs(out)  # DEDUP_V1: kill runaway repeats
        out, _ndup = _dedup_repeated_paragraphs(out)  # ANTI_REPEAT_V2 DEDUP_V2: lettered + cross-section
        out, _nsent = _dedup_repeated_sentences(out)  # L8-DEDUP: sentence-level cross-paragraph dups
        _ndup += _nsent
        # ANTI_REPEAT_V2 RE-ROLL: a heavy verbatim loop (many dup paragraphs
        # dropped) means the section degenerated. Re-roll ONCE at higher
        # temperature to break the deterministic loop. Temperature diversifies
        # sampling WITHOUT corrupting grounded citations (unlike a DRY/repeat
        # penalty, which makes the model garble repeated cites to evade it).
        if _ndup >= 5:
            print(f"  [section:{sect}] loop detected ({_ndup} dups) -> re-roll @ temp 0.5")
            alt = call_llm(prompt, temperature=0.5)
            alt = _clean_llm_output(alt, section=sect) if alt else ""
            alt = _scrub_filename_leaks(alt)
            alt = _dedup_paragraphs(alt)
            alt, _nalt = _dedup_repeated_paragraphs(alt)
            if alt.strip() and (_nalt < _ndup or (_nalt == _ndup and len(alt) > len(out))):
                out = alt
        if out.strip():
            parts.append(out.strip())
        else:
            print(f"  [section:{sect}] empty output")
    return "\n\n".join(parts)
# === END SECTION_BY_SECTION_V1 ===


def _stub_block(stub_text, header_regex):
    """Extract the markdown block under a '## <header>' up to the next '## ' (or EOF). '' if absent."""
    import re as _re
    m = _re.search(r'(?ims)^##\s*(?:' + header_regex + r').*?$(.*?)(?=^##\s|\Z)', stub_text)
    return m.group(1).strip() if m else ""


def _extract_questions(stub_text):
    """Pull the enumerated Questions Involved / Statement of the Issues VERBATIM from the stub
    (Pa.R.A.P. 2116 / FRAP 28(a)(5) require precision — never let the LLM paraphrase them). Returns a
    list of question strings in order."""
    import re as _re
    block = _stub_block(stub_text, r'STATEMENT OF THE QUESTIONS INVOLVED|STATEMENT OF THE ISSUES(?:\s+PRESENTED)?')
    if not block:
        return []
    qs, cur = [], None
    for ln in block.splitlines():
        m = _re.match(r'^\s*(\d+)\.\s+(.*)', ln)
        if m:
            if cur is not None:
                qs.append(cur.strip())
            cur = m.group(2)
        elif cur is not None and ln.strip():
            cur += " " + ln.strip()
    if cur is not None:
        qs.append(cur.strip())
    return qs


def _strip_reasoning_leak(text):
    """Reasoning models leak UNTAGGED scratchpad into the content field (not in <think> tags), so the
    section cleaner misses it — e.g. 'The user wants me to...', 'Wait, I need to...', 'Let's break down',
    'Stub says:'. Cut each such block from its marker to the next real header (## / ###) or EOF. Also drops
    stray outline-scaffolding label lines ('Authority:', 'Factual grounding:', 'Standard of review:' as a
    bare bullet). Never touches finished prose."""
    import re as _re
    if not text:
        return text
    _marker = (r"(?im)^\s*(?:the user wants|wait,|let me\b|let'?s\b|i need to\b|i will use|i should\b|"
               r"i'?ll\b|okay,|now,? i\b|first,? i\b|self-correction|breaking (?:this|it) down|"
               r"let'?s break|let'?s double|stub says:|docket highlights says:|to summarize|my plan\b|"
               r"i must (?:follow|make sure)|here'?s (?:my|the) plan)")
    m = _re.search(_marker, text)
    guard = 0
    while m and guard < 40:
        guard += 1
        nxt = _re.search(r"(?m)^\s*#{1,4}\s+\S", text[m.end():])
        text = (text[:m.start()] + text[m.end() + nxt.start():]) if nxt else text[:m.start()]
        m = _re.search(_marker, text)
    # drop bare outline-label bullets the model emits instead of prose
    text = _re.sub(r"(?im)^\s*[-*]\s*(?:standard of review|preservation|authority|factual grounding|"
                   r"argument|rebuttal|conclusion)\s*:\s*", "", text)
    return text.strip()


def _draft_appellate_sectioned(stub_text, motion_filename, case_id):
    """Assemble a Pa.R.A.P. 2111 (PA_SUPER) / FRAP 28 (3D_CIR) APPELLATE BRIEF: deterministic structure +
    VERBATIM Questions Involved (2116 precision), with the LLM drafting only the Statement of the Case,
    Summary of Argument, and Argument prose. Error-correction posture, not relitigation."""
    court = _appellate_court_for(motion_filename) or 'PA_SUPER'
    court_label, juris_cite, struct_rule, sor_rule = _APPELLATE_RULES.get(
        court, _APPELLATE_RULES['PA_SUPER'])
    # ground the LLM sections in the case audit + real docket (same as the motion path)
    _pr = _load_case_audit(case_id)
    gstub = (stub_text + _pr) if _pr else stub_text
    gstub = _groundize_sources(gstub, case_id)
    # CORPUS-GROUNDED appellate frame (R7: derive the shape from the corpus, don't assert it): what the
    # court REQUIRES (Fed. R. App. P. 28) + how to PROVE each issue (Am Jur 2d Proof of Facts) + the
    # start-to-finish citation ladder incl. the enacting Public Law. Leads gstub so every appellate
    # section (case/summary/argument) drafts against it. Fail-safe: '' just leaves the prompt unchanged.
    try:
        from pipeline.drafter_grounding import get_court_format_grounding, get_public_law_grounding
        _is_state_app = court in ('PA_SUPER', 'PA_SUPREME', 'PA_COMMONWEALTH')
        _fmt = get_court_format_grounding(is_appellate=True, is_state=_is_state_app)  # FRAP 28 (fed) / Pa.R.A.P. (state)
        _pl = get_public_law_grounding(stub_text)  # enacting Public Law / __ Stat. __ for each statute cited
        _pre = "\n\n".join(x for x in (_fmt, _pl) if x)
        if _pre:
            gstub = _pre + "\n\n" + gstub
    except Exception:
        pass

    def _llm_section(sect):
        prompt = _section_prompt(gstub, motion_filename, sect)
        # enable_thinking=False: the m0-gate discipline — the answer lands in `content`, no scratchpad
        # to leak (the complex appellate prompt otherwise dumps its plan into content).
        out = call_llm(prompt, enable_thinking=False)
        out = _clean_llm_output(out, section=sect) if out else ""
        out = _scrub_filename_leaks(out)
        out = _strip_reasoning_leak(out)   # belt-and-suspenders safety net
        try:
            out, _ = _dedup_repeated_paragraphs(out)
        except Exception:
            pass
        return out.strip()

    def _ensure_header(text, header):
        """Prepend the section header if the LLM omitted it (it drafts content but sometimes drops the ##)."""
        if not text:
            return text
        if re.search(r"(?im)^\s*#{1,3}\s*" + re.escape(header.split(' ', 1)[-1] if ' ' in header else header), text):
            return text
        return f"## {header}\n\n" + text.lstrip()

    def _llm_argument_part(n, question):
        """Draft ONE Argument part in FINISHED prose (focused call avoids the outline/reasoning dump the
        single all-issues call produced). Reuses the grounded app_argument prompt, focused on one issue."""
        base = _section_prompt(gstub, motion_filename, "app_argument")
        focus = (
            f"\n\n=== FOCUS: draft ONLY Argument Part {n}, for THIS ONE issue ===\n"
            f"ISSUE {n}: {question}\n\n"
            f"Write this issue SKINNY — condensed, near-list, with the controlling authority ATTACHED to the "
            f"point (NOT a verbose essay). m0 already gathered the law broadly; here you CONDENSE. Format:\n"
            f"  '### {n}. <the point in a few words>'\n"
            f"  '*Standard of review:* <...>  *Preservation:* <...>'\n"
            f"  Then ONE tight paragraph (2-4 sentences MAX) stating the error and why it is error on THIS "
            f"record, and immediately AFTER it the controlling authority as a compact attached list — each "
            f"governing rule/statute/case on its own line: '- <Rule/statute/case cite> — <one clause of what "
            f"it holds or requires>'. The cite carries the weight; do not restate it in prose. Lead with the "
            f"jurisdictional/dispositive authority. "
            f"HARD RULES: 2-4 sentences of prose MAX before the authority list (skinny, not an essay); no "
            f"meta-commentary ('the user wants', 'let me', 'wait', 'stub says'); no task planning; cite only "
            f"federal authority from the grounding/record; never pad. Output ONLY Argument Part {n}."
        )
        out = call_llm(base + focus, enable_thinking=False, max_tokens=1100)  # skinny: force condensation
        out = _clean_llm_output(out, section="app_argument") if out else ""
        out = _scrub_filename_leaks(out)
        out = _strip_reasoning_leak(out)
        out = _dedup_paragraphs(out)          # kill runaway repeats (parts 3/5 ballooned to 29k chars)
        try:
            out, _ = _dedup_repeated_paragraphs(out)
        except Exception:
            pass
        return out.strip()

    parts = []
    # (1) Statement of Jurisdiction. The VEHICLE decision belongs to the LLM gate (m0_appeal_gate,
    # corpus-grounded); the drafter CONSUMES it. Prefer the cached LLM decision; else the gate's
    # deterministic fallback. The cite map lives in the gate (one source of truth) — not duplicated here.
    # DETERMINISTIC + GROUNDED, like reasoning_orchestrator decides the motions: derive the vehicle from
    # CASE_FACTS (via the posture) + the order being appealed. This does NOT drift the way the LLM gate
    # does (it once returned "§ 1292(a)(1) -> Third Circuit" for a PA state case). The LLM decision is
    # consulted ONLY to confirm; it never overrides the grounded court/vehicle.
    _vehicle, _src = "full_appeal_as_of_right", "default"
    try:
        from pipeline import m0_appeal_gate as _ag
        # CONSUME the gate's grounded decision (single source of truth). The gate reconciles the LLM pick
        # with deterministic guards (court validity; § 1292(a)(1) only with an injunction; partial
        # dispositions -> Rule 54(b)). Re-derive locally ONLY if no cached decision exists — the old code
        # always re-derived via detect_appeal_type_deterministic(), which drifted to "collateral_order"
        # for a final-judgment appeal and ignored the gate.
        _cached = None
        try:
            _cached = _ag.load_cached_decision(case_id)
        except Exception:
            _cached = None
        if _cached and _cached.get("primary_vehicle"):
            _vehicle = _cached["primary_vehicle"]
            _src = f"m0 appeal gate ({_cached.get('source', 'cache')})"
        else:
            _vehicle = _ag.detect_appeal_type_deterministic(stub_text, case_id)
            _src = "grounded fallback (no cached gate decision)"
        _basis, _phrase = _ag.jurisdiction_cite(_vehicle, court)
    except Exception as _ge:
        _basis, _phrase = (juris_cite, "from a final judgment or order")
        _src = f"drafter-fallback ({_ge})"
    _juris_stub = _stub_block(stub_text, r'JURISDICTIONAL STATEMENT|STATEMENT OF (?:APPELLATE )?JURISDICTION')
    if _juris_stub:
        parts.append("## STATEMENT OF JURISDICTION\n\n" + _juris_stub)
    else:
        parts.append("## STATEMENT OF JURISDICTION\n\n"
                     f"This Court has jurisdiction under {_basis}, {_phrase}. The notice of appeal was "
                     "timely filed.")
    print(f"  [appellate] vehicle = {_vehicle} via {_src} ({court})")
    # (2) Order(s) in Question — from the stub
    _orders = _stub_block(stub_text, r'ORDERS IN QUESTION')
    if _orders:
        parts.append("## ORDER(S) IN QUESTION\n\n" + _orders)
    # (3) Scope and Standard of Review — deterministic frame (per-Question standards restated in Argument)
    parts.append("## STATEMENT OF THE SCOPE AND STANDARD OF REVIEW\n\n"
                 "The scope of review is plenary. The standard of review is de novo as to questions of law "
                 "and the voidness of an order (a void order is a nullity reviewable at any time), and abuse "
                 "of discretion as to discretionary rulings. The applicable standard is stated at the head of "
                 f"each part of the Argument, as required by {sor_rule}.")
    # (4) Statement of the Questions Involved — VERBATIM from the stub (2116 precision)
    _qs = _extract_questions(stub_text)
    if _qs:
        _q_title = "STATEMENT OF THE ISSUES" if court == '3D_CIR' else "STATEMENT OF THE QUESTIONS INVOLVED"
        q_md = f"## {_q_title}\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(_qs, 1))
        parts.append(q_md)
    # (5) Statement of the Case — LLM (ensure the header if the model dropped it)
    _case = _llm_section("app_case")
    if _case:
        parts.append(_ensure_header(_case, "STATEMENT OF THE CASE"))
    # (6) Summary of Argument — LLM
    _summ = _llm_section("app_summary")
    if _summ:
        parts.append(_ensure_header(_summ, "SUMMARY OF ARGUMENT"))
    # (7) Argument — PER ISSUE (focused calls -> finished prose, one part per Question in order)
    if _qs:
        _arg_parts = ["## ARGUMENT"]
        for _i, _q in enumerate(_qs, 1):
            _p = _llm_argument_part(_i, _q)
            if _p:
                _arg_parts.append(_p)
            print(f"  [appellate] argument part {_i}/{len(_qs)} drafted ({len(_p)} chars)")
        if len(_arg_parts) > 1:
            parts.append("\n\n".join(_arg_parts))
    else:
        _arg = _llm_section("app_argument")
        if _arg:
            parts.append(_ensure_header(_arg, "ARGUMENT"))
    # (8) Conclusion — deterministic from the stub's relief
    _relief = _stub_block(stub_text, r'FRAME / RELIEF|RELIEF')
    concl = "## CONCLUSION\n\nFor the foregoing reasons, Appellant respectfully requests that this Court "
    if 'vacate' in (_relief or stub_text).lower():
        concl += ("VACATE the orders below as void, REVERSE, reinstate the writ of execution, and REMAND to "
                  "a different judge for further proceedings.")
    else:
        concl += "REVERSE the order(s) below and REMAND for further proceedings."
    parts.append(concl)
    print(f"  [appellate] assembled {len(parts)} Pa.R.A.P.-2111 sections for {court} "
          f"({len(_qs)} questions verbatim)")
    return "\n\n".join(p for p in parts if p.strip())

def _correct_cite_by_name(p1, p2, rep):
    """Corpus NAME->CITE. Look the case NAME up in the corpus to get its CORRECT reporter cite (same
    reporter), so a REAL case the LLM gave a WRONG page — e.g. 'Mathews v. Eldridge, 424 U.S. 316' when the
    real cite is 424 U.S. 319 — is FIXED, not stripped. Returns 'VOL REP PAGE' (the most-frequent = landmark
    first-page cite in the corpus) or None if the name is not found (then it is truly fabricated). No raise."""
    try:
        import sqlite3
        from collections import Counter
        from pipeline.case_comprehension import CHUNK_DB
        name = f"{p1} v. {p2}"
        repcore = re.escape(rep).replace(r"\.", r"\.?\s*")
        con = sqlite3.connect(str(CHUNK_DB))
        rows = con.execute("select text from chunk_text_fts where chunk_text_fts match ? limit 12",
                           ('"' + re.sub(r"[^A-Za-z ]", " ", name).strip() + '"',)).fetchall()
        con.close()
        pat = re.compile(re.escape(p1) + r"\s+v\.?\s+" + re.escape(p2) + r",?\s+(\d+)\s+" + repcore + r"\s+(\d+)", re.I)
        cnt = Counter()
        for (t,) in rows:
            for mm in pat.finditer(t or ""):
                cnt[(mm.group(1), mm.group(2))] += 1
        if not cnt:
            return None
        (vol, page), _ = cnt.most_common(1)[0]
        return f"{vol} {rep} {page}"
    except Exception:
        return None


def _verify_draft_cites(md):
    """CITE HARD-RULE (restored 2026-07-06): every case cite the LLM writes must be a REAL case whose
    NAME matches the reporter cite. Verify each (Name v. Name, VOL REP PAGE) against CourtListener via the
    existing pipeline.cite_authority_check (v4-fixed) — the SAME verifier, not a parallel one. Strip any
    cite whose name does NOT match the real case at that reporter cite (the fabrication mode: real volume,
    fake name — e.g. 'Hicks v. Feinberg, 533 F.3d 143' where 533 F.3d 143 is really United States v.
    Finnerty). Runs at assembly, so a fabricated cite is caught BEFORE the draft is written. If CourtListener
    cannot confirm (offline / not found), the cite is KEPT and flagged — never strip on an infrastructure gap.
    Returns (clean_md, stripped_list). Never raises."""
    if not md:
        return md, []
    try:
        from pipeline import cite_authority_check as _cac
    except Exception as _e:
        print(f"  [cite-verify] skipped (verifier unavailable: {_e})")
        return md, []
    # HARVARD-FIRST forum (2026-08-01): infer the controlling forum so the CAP-first check can also report
    # BINDING vs NON-BINDING. Best-effort from the draft text; defaults to the federal 3d Cir (EDPA).
    _mdl = (md or "").lower()
    if "court of common pleas" in _mdl or "pa. r." in _mdl or "pa.r.c.p" in _mdl:
        _forum = "PA_CP"
    else:
        _forum = "EDPA"
    _E = r"[*_`]*"   # tolerate markdown emphasis around the case name (the model italicizes cites)
    _REP = r"(U\.\s?S\.|S\.\s?Ct\.|F\.\s?\d?[a-z]?|F\.\s?Supp\.\s?\d?[a-z]?|A\.\s?\d?[a-z]?)"
    pat = re.compile(
        r"(?:See,?\s*(?:e\.g\.,?\s*)?|accord\s+|cf\.\s+)?"
        + _E + r"([A-Z][A-Za-z.'&-]+(?:\s+[A-Z][A-Za-z.'&-]+){0,5})\s+v\.\s+"
        r"([A-Z][A-Za-z.'&-]+(?:\s+[A-Z][A-Za-z.'&-]+){0,5})" + _E +
        r",?\s+(\d+)\s+" + _REP + r"\s+(\d+)"
        r"(?:,\s*\d+)?"            # optional pincite
        r"(?:\s*\([^)]*\))?"       # optional (court year)
        r"\.?")

    def _norm(s):
        return re.sub(r"[^a-z]", "", (s or "").lower())

    stripped, corrected, unconfirmed, checked, nonbinding = [], [], [], set(), []
    for m in pat.finditer(md):
        p1, p2, vol, rep, page = m.group(1), m.group(2), m.group(3), m.group(4).replace(" ", ""), m.group(5)
        key = f"{vol}|{rep}|{page}|{_norm(p1)}|{_norm(p2)}"
        if key in checked:
            continue
        checked.add(key)
        # HARVARD-FIRST (2026-08-01): try Caselaw Access Project (static.case.law — free, NO rate limit,
        # returns the full opinion + the COURT) BEFORE CourtListener. This (a) removes the CL rate-limit
        # blind spot that was keeping unverified cites, and (b) yields BINDING vs NON-BINDING for free, so
        # a real-but-out-of-circuit case dressed as controlling is visible. CAP-miss -> the existing CL /
        # Justia / Scholar / corpus chain, unchanged. Never raises.
        r = None
        _binding = ""
        try:
            from pipeline.case_fetch import case_authority as _ca
            _a = _ca(f"{p1} v. {p2}", f"{vol} {rep} {page}", target_court=_forum)
            if _a.get("found"):
                r = {"found": True, "case_name": _a.get("name") or "", "court": _a.get("court") or "",
                     "source": "cap"}
                _binding = _a.get("binding") or ""
        except Exception:
            r = None
        if not (isinstance(r, dict) and r.get("found")):
            try:
                r = _cac.lookup_courtlistener(rep, int(vol), int(page))
            except Exception:
                r = None
        # FALLBACK CHAIN: if CourtListener was rate-limited / errored / didn't find it, try the other
        # sources the verifier already has — Justia (SCOTUS 'U.S.' cites) then Google Scholar — so a CL
        # limit/outage doesn't blind the check. Only a source that AFFIRMATIVELY finds the cite counts.
        _cl_infra = isinstance(r, dict) and not r.get("found") and bool(r.get("note"))
        if (not isinstance(r, dict) or not r.get("found")):
            try:
                if rep.upper().startswith("U.S") and hasattr(_cac, "lookup_justia_scotus"):
                    _j = _cac.lookup_justia_scotus(int(vol), int(page))
                    if isinstance(_j, dict) and _j.get("found"):
                        r = _j
            except Exception:
                pass
        if (not isinstance(r, dict) or not r.get("found")) and hasattr(_cac, "lookup_google_scholar"):
            try:
                _g = _cac.lookup_google_scholar(f"{vol} {rep} {page}")
                if isinstance(_g, dict) and _g.get("found"):
                    r = _g
                elif _cl_infra:
                    print(f"  [cite-verify] {vol} {rep} {page}: CourtListener {r.get('note')} + Scholar "
                          f"unconfirmed — KEPT for manual review (no source could confirm).")
            except Exception:
                pass
        if not isinstance(r, dict) or not r.get("found"):
            # L6: no ONLINE source confirmed it. Fall back to the CORPUS (corpus = verification): if the case
            # name resolves to a cite in the corpus, keep it (corpus-confirmed); otherwise mark it inline
            # [UNVERIFIED CITE] so an offline / rate-limited run is NOT indistinguishable from a verified one.
            if _correct_cite_by_name(p1, p2, rep):
                continue   # corpus confirms the case -> keep, verified by corpus
            unconfirmed.append(m.group(0))
            continue
        real = r.get("case_name") or ""
        if not any(_norm(p) and _norm(p) in _norm(real) for p in (p1, p2)):
            # The reporter cite points to a DIFFERENT case. Before discarding a possibly-REAL case that just
            # has a WRONG page, look the NAME up in the corpus for its CORRECT cite and FIX the page. Only
            # strip if the name itself is not in the corpus (truly fabricated). Fix-don't-reject: a real case
            # with a bad pincite is a powerful authority we should keep, corrected.
            fixed = _correct_cite_by_name(p1, p2, rep)
            if fixed and fixed.replace(" ", "") != f"{vol} {rep} {page}".replace(" ", ""):
                cs, ce = m.start(3) - m.start(0), m.end(5) - m.start(0)
                corrected.append({"span": m.group(0), "new_span": m.group(0)[:cs] + fixed + m.group(0)[ce:],
                                  "name": f"{p1} v. {p2}", "old": f"{vol} {rep} {page}", "new": fixed})
            else:
                stripped.append({"cited": f"{p1} v. {p2}", "cite": f"{vol} {rep} {page}",
                                 "real": real, "span": m.group(0)})
        elif _binding == "NON-BINDING":
            # real, correctly-cited case, but NOT controlling in this forum — surface it so an
            # out-of-circuit / district opinion is not silently relied on as if it were binding.
            nonbinding.append({"name": f"{p1} v. {p2}", "cite": f"{vol} {rep} {page}",
                               "court": (r.get("court") or "")[:48]})

    out = md
    for nb in nonbinding:
        print(f"  [cite-verify] NON-BINDING here (persuasive only): {nb['name']}, {nb['cite']} "
              f"— {nb['court']}")
    for span in unconfirmed:                       # L6: make offline/unconfirmed cites visible to the fileability gate
        if "[UNVERIFIED CITE]" not in span:
            out = out.replace(span, span + " [UNVERIFIED CITE]", 1)
    for c in corrected:
        out = out.replace(c["span"], c["new_span"], 1)
        print(f"  [cite-verify] CORRECTED cite (real case, wrong page): '{c['name']}' {c['old']} -> {c['new']}")
    for s in stripped:
        # remove the exact matched span (name + emphasis + reporter cite + pincite + parenthetical)
        out = out.replace(s["span"], "", 1)
        print(f"  [cite-verify] STRIPPED fabricated cite: '{s['cited']}, {s['cite']}' "
              f"— {s['cite']} is really {s['real']}")
    if not stripped and not corrected:
        print(f"  [cite-verify] {len(checked)} unique case cite(s) checked — all verified (or CL-unconfirmed, kept).")
    return out, stripped


def assemble_full_draft(stub_text, llm_output, case_id, motion_filename):
    """Assemble title + body (+ IF/THEN logic chain) + sig block.

    NOTE: caption/court-header are now built by the renderer
    (md_to_docx.render_md_to_docx, RENDER_MD_CAPTION_V1) from ctx, so the body
    no longer emits its own caption table / court header / provenance lines."""
    # INTRO_PRESERVE_V1: each section was already cleaned in _draft_sectioned.
    # Re-cleaning with the default section="facts" would truncate to the first
    # STATEMENT OF FACTS header and DROP the assembled '## INTRODUCTION'. Pass a
    # non-"facts" section so the facts-truncation branch is skipped; the
    # <think>/binding strips still run and the full body is preserved.
    llm_output = _clean_llm_output(llm_output, section="assembled")  # strip leak, keep intro
    # === FORMAL_TITLE_V1: formal pleading title keyed on filing_type
    _ft_key = re.sub(r'^\d+_', '', motion_filename.replace('.md', ''))
    _ft_key = re.sub(r'_DRAFT$', '', _ft_key)
    _ft_key = re.sub(r'_dkt\d+$', '', _ft_key)   # L-TITLE: drop the docket suffix so it maps to a canonical
    #   title (was leaving 'opposition_..._dkt174' -> slug-uppercase 'OPPOSITION ... DKT174' in the filing)
    # APPELLATE_CAPTION_V1: pass filing_type + appeal number so appellate
    # filings get the Court of Appeals caption (data already present).
    _appeal_no = _lookup_appeal_number(case_id)
    cap = caption_for_case(case_id or '?', filing_type=_ft_key, appeal_number=_appeal_no)
    _RULE_TITLE_FIX = {
        'rule_54b_certification': "PLAINTIFF'S MOTION FOR CERTIFICATION UNDER RULE 54(b)",
        'motion_to_certify_54b': "PLAINTIFF'S MOTION FOR CERTIFICATION UNDER RULE 54(b)",
        'rule_60b_motion': "PLAINTIFF'S MOTION FOR RELIEF UNDER RULE 60(b)",
        'rule_60b_motion_1to3': "PLAINTIFF'S MOTION FOR RELIEF UNDER RULE 60(b)(1)-(3)",
        'rule_60b4_motion': "PLAINTIFF'S MOTION TO VACATE VOID JUDGMENT UNDER RULE 60(b)(4)",
        'rule_60d3_fraud_on_court': "PLAINTIFF'S MOTION FOR RELIEF FOR FRAUD ON THE COURT UNDER RULE 60(d)(3)",
        'motion_for_default_judgment': "PLAINTIFF'S MOTION FOR DEFAULT JUDGMENT",
        'motion_for_summary_judgment': "PLAINTIFF'S MOTION FOR SUMMARY JUDGMENT",
        'motion_for_sanctions': "PLAINTIFF'S MOTION FOR SANCTIONS",
        'motion_to_compel_discovery': "PLAINTIFF'S MOTION TO COMPEL DISCOVERY",
        'amended_complaint': "PLAINTIFF'S AMENDED COMPLAINT",
        'omnibus_exposure_filing': "PLAINTIFF'S OMNIBUS EXPOSURE FILING — STATEMENT OF SYSTEMIC VIOLATIONS",
        'opposition_to_motion_to_dismiss': "PLAINTIFF'S OPPOSITION TO MOTION TO DISMISS",
        'notice_of_appeal_reserved': "PLAINTIFF'S NOTICE OF APPEAL",
        'amended_notice_of_appeal': "AMENDED NOTICE OF APPEAL",
        'opposition_dkt172_judgment_on_pleadings': "PLAINTIFF'S RESPONSE IN OPPOSITION TO DEFENDANT'S MOTION FOR JUDGMENT ON THE PLEADINGS",
        'opposition_to_judgment_on_the_pleadings': "PLAINTIFF'S RESPONSE IN OPPOSITION TO DEFENDANT'S MOTION FOR JUDGMENT ON THE PLEADINGS",
        'opposition_to_motion_for_summary_judgment': "PLAINTIFF'S RESPONSE IN OPPOSITION TO DEFENDANT'S MOTION FOR SUMMARY JUDGMENT",
        'opposition_to_motion_for_sanctions': "PLAINTIFF'S RESPONSE IN OPPOSITION TO DEFENDANT'S MOTION FOR SANCTIONS",
        'motion_for_entry_of_default': "PLAINTIFF'S MOTION FOR ENTRY OF DEFAULT",
        'motion_for_recusal': "PLAINTIFF'S MOTION TO DISQUALIFY/RECUSE",
        'motion_to_strike': "PLAINTIFF'S MOTION TO STRIKE",
        'motion_for_reconsideration': "PLAINTIFF'S MOTION FOR RECONSIDERATION",
        'motion_for_statement_of_reasons': "PLAINTIFF'S MOTION FOR A STATEMENT OF REASONS AND FINDINGS OF FACT AND CONCLUSIONS OF LAW",
        'motion_1292b_interlocutory_appeal': "PLAINTIFF'S MOTION TO CERTIFY ORDER FOR INTERLOCUTORY APPEAL UNDER 28 U.S.C. 1292(b)",
        'frap_21_petition_for_writ_of_mandamus': "PETITION FOR WRIT OF MANDAMUS UNDER FRAP 21",
        'motion_to_reinstate_appeal': "MOTION TO REINSTATE APPEAL",
        'motion_to_recall_mandate': "MOTION TO RECALL MANDATE",
        'frap_40_panel_rehearing': "PETITION FOR PANEL REHEARING UNDER FRAP 40",
        'frap_40_rehearing_en_banc': "PETITION FOR REHEARING EN BANC UNDER FRAP 35",
        'scotus_cert_petition': "PETITION FOR WRIT OF CERTIORARI",
        'appellate_brief_superior_court': "BRIEF OF APPELLANT",
        'appellate_brief_pa_super': "BRIEF OF APPELLANT",
        'appellate_brief_3rd_cir': "BRIEF OF APPELLANT",
        'appellate_reply_brief': "REPLY BRIEF OF APPELLANT",
        'appellate_reply_brief_pa_super': "REPLY BRIEF OF APPELLANT",
    }
    if _ft_key in _RULE_TITLE_FIX:
        motion_title = _RULE_TITLE_FIX[_ft_key]
    else:
        try:
            from generation.reply_brief_drafter import _motion_title_for as _mtf
            motion_title = _mtf(_ft_key)
        except Exception:
            motion_title = re.sub(r'^\d+\s+', '', motion_filename.replace('.md','').replace('_',' ')).title().upper()
    header = f"# {motion_title}\n\n"
    # === END FORMAL_TITLE_V1 ===

    # APPELLATE ASSEMBLY: an appellate brief is error-correction, not a motion. It gets NO IF/THEN logic
    # chain, NO [PROPOSED] ORDER, and NO stub-echo appendix; its signature block omits the proposed order,
    # and the Appendix / Reproduced Record come from the brief body (per the stub). Certificate of Service
    # per the appellate rules.
    if _is_appellate_filing(motion_filename):
        _app_sig = (
            f"\n\nRespectfully submitted,\n\n"
            f"/s/ FILER NAME\n_________________________\n"
            f"{PLAINTIFF_NAME}, Appellant, *sui juris*\n"
            f"123 Main St, Suite 100\nAnytown, ST 00000\n\n"
            f"Dated: {datetime.now().strftime('%B %-d, %Y')}\n\n"
            f"[[PAGEBREAK]]\n\n## CERTIFICATE OF SERVICE\n\n"
            f"I, {PLAINTIFF_NAME}, certify that on the date set forth below a true and correct copy of the "
            f"foregoing Brief was served upon all counsel and parties of record by U.S. Mail at the addresses "
            f"on file, in compliance with the applicable Rules of Appellate Procedure.\n\n"
            f"/s/ FILER NAME\n_________________________\n"
            f"{PLAINTIFF_NAME}, Appellant, *sui juris*\n\n"
            f"Dated: {datetime.now().strftime('%B %-d, %Y')}\n"
        )
        _full, _ = _verify_draft_cites(header + llm_output + _app_sig)
        return _full

    # IF/THEN logic chain (additive) — engine-built from the stub authority+facts.
    ifthen = _ifthen_block_for(stub_text, motion_title, case_id)
    ifthen_section = ("\n\n" + ifthen + "\n") if ifthen else ""

    # PROPOSED_ORDER_V2: an OPPOSITION/RESPONSE asks the court to DENY the opposed motion; a plaintiff's own
    # motion asks the court to GRANT. Branch on the formal title so the proposed order matches the filing.
    _mt_up = (motion_title or "").upper()
    if "OPPOSITION" in _mt_up or "RESPONSE IN OPPOSITION" in _mt_up:
        _proposed_order_clause = ("AND NOW, this _____ day of __________, 20____, upon consideration of the "
                                  "Motion and Plaintiff's response in opposition thereto, it is hereby ORDERED "
                                  "that the Motion is DENIED.")
    else:
        _proposed_order_clause = ("AND NOW, this _____ day of __________, 20____, upon consideration of the "
                                  "foregoing Motion, and any response thereto, it is hereby ORDERED that the "
                                  "Motion is GRANTED.")

    sig = (
        f"\n\nRespectfully submitted,\n\n"
        f"/s/ FILER NAME\n"
        f"_________________________\n"
        f"{PLAINTIFF_NAME}, *sui juris*\nAll rights reserved without prejudice; 13 Pa.C.S. \u00a7 1308 / U.C.C. \u00a7 1-308\nAppearing personally under 28 U.S.C. \u00a7 1654\n"
        f"123 Main St, Suite 100\n"
        f"Anytown, ST 00000\n\n"
        f"Dated: {datetime.now().strftime('%B %-d, %Y')}\n\n"
        f"[[PAGEBREAK]]\n\n"
        f"## CERTIFICATE OF SERVICE\n\n"
        f"I, {PLAINTIFF_NAME}, appearing personally under 28 U.S.C. \u00a7 1654, hereby certify that on the date set forth "
        f"below, a true and correct copy of the foregoing was served on "
        f"all parties of record by **first-class United States Mail, postage prepaid,** "
        f"at the addresses on file with the Court. I am not a registered electronic filer; "
        f"service is made by mail.\n\n"
        f"/s/ FILER NAME\n"
        f"_________________________\n"
        f"{PLAINTIFF_NAME}, *sui juris*\n\n"
        f"Dated: {datetime.now().strftime('%B %-d, %Y')}\n\n"
        f"[[PAGEBREAK]]\n\n"
        f"## [PROPOSED] ORDER\n\n"
        f"{_proposed_order_clause}\n\n"
        f"BY THE COURT:\n\n"
        f"_________________________\n"
        f"                                                          J.\n"
    )

    # Stub appendix retained for the review copy (finalize strips it for FILING).
    appendix = (
        f"\n---\n\n"
        f"# APPENDIX A - Source Stub (for drafter reference)\n\n"
        f"```markdown\n{stub_text}\n```\n"
    )

    _full, _ = _verify_draft_cites(header + llm_output + ifthen_section + sig + appendix)
    return _full


# ---------- per-stub processing ----------
def process_stub(stub_path, dry_run=False):
    stub_path = Path(stub_path).resolve()
    print(f"\n== Processing {stub_path.name} ==")
    stub_text = stub_path.read_text(encoding='utf-8')
    # STUB_SANITIZE_V1: strip placeholder tokens (Batch 5a)
    stub_text, _stub_sanitize_hits = _sanitize_stub_text(stub_text)
    if _stub_sanitize_hits:
        print(f"[stub-sanitize] {stub_path.name}: {_stub_sanitize_hits}")
    case_id = extract_case_id_from_stub(stub_path)
    print(f"  case_id (inferred): {case_id}")

    # FILING-STATUS GATE: don't re-draft a motion already on the docket. Consolidated PDF == mailed
    # (user's rule), corroborated by docket/email notifications; a trial motion once the case is on
    # appeal is a PRIOR phase. Skip 'filed'; warn on 'consolidated'; draft 'active'/'not_started'.
    # Override with BEEZLE_REDRAFT_FILED=1.
    try:
        import os as _os
        from datetime import date as _date
        from pipeline import filing_status as _fstat
        _slug = case_id_to_folder(case_id) if case_id else ""
        if _slug:
            _st = _fstat.stub_status(_slug, stub_path.name, _date.today().isoformat())
            # WARN, never silently block (a case can be on appeal AND litigating below at once, so a
            # phase-based hard-skip would wrongly drop a live district motion). Only skip if the operator
            # explicitly opts in via BEEZLE_SKIP_FILED=1.
            if _st["status"] in ("filed", "consolidated"):
                print(f"  [filing-gate] NOTE: '{_st['key']}' already consolidated ({len(_st['pdf'])} pdf) "
                      f"(phase={_st['phase']}, status={_st['status']}) — likely already mailed; re-drafting. "
                      f"corrob={_st['corroboration']}")
                if _os.environ.get("BEEZLE_SKIP_FILED") == "1":
                    print("  [filing-gate] SKIP (BEEZLE_SKIP_FILED=1).")
                    return None
            elif _st["status"] == "active":
                print(f"  [filing-gate] ACTIVE: '{_st['key']}' is the current-phase filing "
                      f"(phase={_st['phase']}).")
    except Exception as _fg:
        print(f"  [filing-gate] skipped (non-fatal): {_fg}")

    user_prompt = build_user_prompt(stub_text, stub_path.name)
    # v6b: Black's-anchored grounding injection at call site
    _grounding = get_section_grounding(stub_text, top_k=8)   # broadened: surface case-holding text, not memory-cites
    if _grounding:
        user_prompt = _grounding + "\n\n" + user_prompt

    if dry_run:
        print("  [dry-run] would send prompt:")
        print("  " + "\n  ".join(user_prompt.splitlines()[:15]))
        print(f"  ... ({len(user_prompt)} chars total)")
        return None

    print(f"  calling LLM at {LLAMA_URL} (section-by-section) ...")
    t0 = time.time()
    try:
        llm_output = _draft_sectioned(stub_text, stub_path.name, case_id)
    except Exception as e:
        print(f"  [x] sectioned draft failed ({e}); falling back to single-shot")
        try:
            llm_output = call_llm(user_prompt)
        except Exception as e2:
            print(f"  [x] LLM call failed: {e2}")
            return None
    elapsed = time.time() - t0
    print(f"  LLM responded in {elapsed:.1f}s "
          f"({len(llm_output)} chars output)")

    full = assemble_full_draft(stub_text, llm_output, case_id,
                                stub_path.name)
    full = _dedup_top_level_sections(full)  # DEDUP_SECTIONS_V1: drop a doubled top-level section (re-roll)
    full = _scrub_grounding_leaks(full)      # strip leaked grounding-scaffold sections (render_packet's
                                             # "Scanner-corroborated facts (use for fact section)" / "Drafter
                                             # checklist" etc. copied verbatim by the LLM into the filing)
    full, _fdn = _dedup_repeated_sentences(full)  # FINAL whole-draft prose dedup: catches a sentence repeated
                                                  # ACROSS sections (argument + facts/defects) that the per-section
                                                  # passes miss. Table/heading/list-safe (skips |, #, list markers).
    # ENACTMENT-CLAUSE FOOTNOTES (deterministic): append the enacting Public Law / __ Stat. __ for every
    # statute the FINAL draft cites — the positive-law rung the skinny prose omits — so the citation ladder
    # is complete start-to-finish in the notes and cannot be attacked as incompletely grounded. Fail-safe.
    try:
        from pipeline.drafter_grounding import enacting_law_footnotes as _elf
        _fn = _elf(full)
        if _fn and "ENACTING PUBLIC LAWS" not in full:
            full = full.rstrip() + _fn
    except Exception:
        pass
    # === CITE-SUPPORT GATE (permanent, draft-time): verify every cite STANDS FOR its point and correct the
    # non-SUPPORTED ones BEFORE the draft is finalized (hybrid: swap to a corpus-confident authority; else strip
    # the wrong cite + inline [CITE REVIEW]; OVERSTATED keeps its cite + [CLAIM REVIEW]). Reuses
    # pipeline.claim_support_check (no duplication); the learning crons yield via flow_lock so it never contends.
    # Off with BEEZLE_CITE_GATE=0. ===
    try:
        import os as _osg
        # ON by default: the upstream defect->authority binding only covers DETECTED-defect cites; the CASE cites
        # the model writes freely (arguments, standards) are NOT bound, so it can cite from memory. This gate
        # verifies EVERY cite stands for its point and auto-corrects/flags the non-SUPPORTED ones at draft time —
        # the actual guard against invented cites. Disable only for a deliberate fast pass with BEEZLE_CITE_GATE=0.
        if _osg.environ.get("BEEZLE_CITE_GATE", "1") != "0":
            from pipeline.claim_support_check import correct_draft as _correct_cites
            full, _cgrep = _correct_cites(full)
            _cgfix = [x for x in _cgrep if x.get("action") != "not-located"]
            if _cgfix:
                print(f"  [cite-gate] corrected {len(_cgfix)} cite(s) of {len(_cgrep)} checked:")
                for x in _cgfix[:12]:
                    print(f"             {x['verdict']:11} {x['auth'][:30]:30} -> {x['action'][:46]}")
            else:
                print(f"  [cite-gate] all {len(_cgrep)} cited claims stand for their point")
    except Exception as _cge:
        print(f"  [cite-gate] skipped: {_cge}")
    # === END CITE-SUPPORT GATE ===
    out_path = stub_path.with_name(stub_path.stem + '_DRAFT.md')
    # ----- BEEZLE SCHEMA HOOK 2026-06-09 -----
    try:
        from pipeline.schema_validator import SchemaValidator as _BeezleSV
        _bv = _BeezleSV()
        # Derive filing_type from out_path filename, no re/Path dependency
        _bname = str(out_path).rsplit('/', 1)[-1].rsplit('.', 1)[0].lower()
        while _bname and _bname[0].isdigit():
            _bname = _bname[1:]
        if _bname.startswith('_'):
            _bname = _bname[1:]
        for _bsuf in ['_fed_', '_phila_ccp_', '_3d_cir_', '_scotus_', '_pa_super_']:
            if _bsuf in _bname:
                _bname = _bname.split(_bsuf)[0]
                break
        _bctx = {'case_id': case_id, 'filing_type': _bname}
        _brep = _bv.validate_markdown(full, _bctx, source_path=str(out_path))
        _bv.log_report(_brep)
        if _brep['hard_gaps']:
            _bmark = ''
            for _bg in _brep['hard_gaps']:
                # OMNIBUS_FIX_V1: SCHEMA GAP inline injection disabled (sidecar _review/schema_gaps.jsonl still written)
                pass
            for _bg in _brep['hard_gaps']:
                print('           ' + _bg['block_id'].ljust(35)
                      + ' ' + _bg['validator'].ljust(32)
                      + ' ' + _bg['gap'][:80])
        else:
            print('  [schema] PASS  blocks=' + str(len(_brep['blocks_seen'])))
    except Exception as _be:
        print('  [schema] validator skipped: ' + str(_be))
    # ----- END BEEZLE SCHEMA HOOK -----
    # ----- BEEZLE LEAK GATE 2026-06-13 (P4) -----
    try:
        from pipeline.draft_leak_gate import gate_draft, case_slug_from_case_id
        _case_slug = case_slug_from_case_id(case_id)
        _lclean, _lflags, _lsummary = gate_draft(
            full, _case_slug, draft_path=out_path)
        if _lclean:
            print('  [leak]   clean')
        else:
            print('  [leak]   ' + _lsummary)
    except Exception as _le:
        print('  [leak]   gate skipped: ' + str(_le))
    # ----- END BEEZLE LEAK GATE -----
    _write_draft_safe(out_path, full)
    print(f"  [OK] wrote {out_path}")
    # === CLAIM-SUPPORT PROOF SHEET (non-blocking, non-destructive) ===
    # The twin of _verify_draft_cites: that confirms a cite EXISTS; this confirms each cited authority
    # STANDS FOR the proposition it's attached to, against the corpus, and emits <draft>_PROOF_SHEET.md
    # (claim -> controlling quote -> SUPPORTED/OVERSTATED/WRONG_POINT/UNBACKED). Runs DETACHED so a slow
    # LLM verdict loop can never block or break the draft. Disable with BEEZLE_PROOF_SHEET=0.
    try:
        import os as _os, sys as _sys, subprocess as _sp
        if _os.environ.get("BEEZLE_PROOF_SHEET", "1") != "0":
            _sp.Popen([_sys.executable, "-m", "pipeline.claim_support_check", str(out_path)],
                      cwd=str(REPO), stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            print(f"  [proof] claim-support check launched -> {out_path.stem}_PROOF_SHEET.md")
    except Exception as _pe:
        print(f"  [proof] skipped: {_pe}")
    # === END CLAIM-SUPPORT ===
    # === RENDER_DOCX_NEXT_STEP_V1: same renderer compose_and_draft uses ===
    try:
        _render_docx_for_draft(out_path, case_id)
    except Exception as _de:
        print(f"  [docx] render skipped: {_de}")
    # === END RENDER_DOCX_NEXT_STEP_V1 ===
    return out_path


def _load_ctx_for_case(case_id):
    import yaml as _yaml
    gt = REPO / 'obsidian_sync' / 'cases' / case_id_to_folder(case_id) / 'meta' / 'CASE_GROUND_TRUTH.yaml'
    if gt.exists():
        try:
            return _yaml.safe_load(gt.read_text(encoding='utf-8')) or {}
        except Exception:
            return {}
    return {}


def _strip_appendix(md):
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip('# ').strip().upper().startswith('APPENDIX A'):
            j = i
            while j - 1 >= 0 and not lines[j - 1].strip():
                j -= 1
            if j - 1 >= 0 and lines[j - 1].strip() == '---':
                j -= 1
            return '\n'.join(lines[:j]).rstrip() + '\n'
    return md


def _strip_work_product(md):
    """Render-side defense: no Obsidian/work-product scaffolding may reach a court FILING. Drops any line that
    is a pure work-product annotation carrying a [[wikilink]] (e.g. the '*Grounding: ... [[CASE_FACTS]] ...*'
    backlink drafts append for reviewer traceability), removes a now-orphaned trailing '---' rule, and unwraps
    any stray [[X]] -> X so double-brackets can never print in a filing. Systemic (not per-case): every draft
    renders through here, so this class of leak is closed regardless of which drafter emitted it."""
    import re as _re
    lines = md.splitlines()
    keep = []
    for ln in lines:
        s = ln.strip()
        # A standalone italic/plain annotation line that contains a wikilink is scaffolding -> drop it entirely.
        if '[[' in s and (s.startswith('*') or s.lower().startswith(('grounding', '_grounding', '> '))):
            continue
        keep.append(ln)
    # Drop a trailing '---' horizontal rule left dangling once its annotation was removed.
    while keep and keep[-1].strip() in ('', '---'):
        if keep[-1].strip() == '---':
            keep.pop()
            break
        keep.pop()
    out = '\n'.join(keep)
    # Neutralize any remaining wikilink markup -> display text (defense in depth; should be none after the above).
    out = _re.sub(r'\[\[(?!PAGEBREAK\]\])(?:[^\]|]+\|)?([^\]|]+)\]\]', r'\1', out)
    return out.rstrip() + '\n'


def _render_docx_for_draft(draft_md_path, case_id):
    from generation.md_to_docx import render_md_to_docx
    md = _strip_work_product(_strip_appendix(Path(draft_md_path).read_text(encoding='utf-8')))
    ctx = _load_ctx_for_case(case_id)
    court = 'EDPA'
    try:
        court = (ctx.get('court', {}) or {}).get('id') or 'EDPA'
    except Exception:
        court = 'EDPA'
    # APPELLATE_RENDER_COURT_V1: appellate filing types render under the Court of
    # Appeals (or SCOTUS), not the trial court. Derive filing_type from filename.
    _ftk = re.sub(r'^\d+_', '', Path(draft_md_path).stem.replace('_DRAFT', '').replace('_FILING', ''))
    _APP3D = {'appellate_brief_3rd_cir', 'appellate_reply_brief',
              'frap_21_petition_for_writ_of_mandamus', 'petition_for_writ_of_mandamus',
              'frap_40_panel_rehearing', 'frap_40_rehearing_en_banc',
              'motion_to_recall_mandate', 'motion_to_reinstate_appeal'}
    _SCOTUS = {'scotus_cert_petition'}
    _PASUP = {'appellate_brief_pa_super', 'appellate_brief_superior_court',
              'appellate_reply_brief_pa_super'}
    if _ftk in _SCOTUS:
        court = 'SCOTUS'
    elif _ftk in _APP3D:
        court = '3D_CIR'
    elif _ftk in _PASUP:
        court = 'PA_SUPER'
    # APPELLATE_DOCKET_CTX_V1: for appellate filings, inject the appeal number
    # so the caption table prints the real docket (No. 25-2960) beside the
    # district number, instead of a blank 'No. ____'.
    if court in ('3D_CIR', 'SCOTUS', 'PA_SUPER'):
        _appno = _lookup_appeal_number(case_id)
        if _appno:
            if not isinstance(ctx, dict):
                ctx = {}
            ctx['appellate_docket'] = _appno
            _ci = ctx.get('court')
            if isinstance(_ci, dict):
                _ci['appellate_docket'] = _appno
    # DEFINITIONS: make the FILED draft self-defining (Schedule A + tagged terms) so no ambiguity stands.
    # Wrapped so it can NEVER break a draft; conservative (inserts schedule + tags, no blind rewrites).
    # QUANTUM FILING-TYPE GATE: footnoted terms-of-art belong on ARGUMENT filings (motions, briefs,
    # oppositions, replies, petitions) — NOT on administrative docs (cover sheets, proposed orders,
    # certificates, praecipes, entries of appearance, exhibit covers) where superscript defs are noise
    # or improper. Negative list so any NEW argument filing type is tagged automatically.
    _QUANTUM_SKIP = {
        'civil_cover_sheet', 'cover_sheet', 'proposed_order', 'order',
        'certificate_of_service', 'certificate_of_compliance', 'proof_of_service',
        'praecipe', 'praecipe_to_enter_judgment', 'praecipe_for_writ',
        'entry_of_appearance', 'notice_of_appeal', 'exhibit_cover', 'exhibit_index',
        'summons', 'ifp_application', 'application_to_proceed_ifp',
    }
    _skip_quantum = (_ftk in _QUANTUM_SKIP) or _ftk.startswith(("notice_of_appeal", "amended_notice_of_appeal",
                     "proposed_order", "certificate_of", "praecipe", "civil_cover", "entry_of_appearance"))
    try:
        from pipeline import definitions_tagger
        # Administrative filings (notice of appeal, cert of service, praecipe, cover sheet, IFP form) are
        # short jurisdictional/form documents — they get NEITHER the Schedule A definitions NOR the quantum
        # footnotes. Argument filings (motions, oppositions, briefs) keep BOTH (user pref).
        if _skip_quantum:
            print(f"  [definitions] skipped (administrative filing type: {_ftk})")
        else:
            md = definitions_tagger.inject_into_draft(md, case_id)                       # Schedule A
            md = definitions_tagger.inject_quantum(md, case_id, ground=False, sup="unicode")  # superscripts + footnote block
    except Exception as _de:
        print(f"  [definitions-inject] skipped (non-fatal): {_de}")
    # APPELLATE LENGTH RULE: principal briefs are word-count limited. Append a Certificate of Compliance
    # stating the actual count, and WARN loudly if over the governing cap so it can be trimmed before filing.
    #   PA_SUPER  Pa.R.A.P. 2135(a)(1): principal <= 14,000 words
    #   3D_CIR    FRAP 32(a)(7)(B)(i): principal  <= 13,000 words
    #   SCOTUS    Sup. Ct. R. 33.1(g): merits <= 13,000 words
    _WORD_CAPS = {'PA_SUPER': (14000, 'Pa.R.A.P. 2135(a)(1)'),
                  '3D_CIR': (13000, 'Fed. R. App. P. 32(a)(7)(B)(i)'),
                  'SCOTUS': (13000, 'Sup. Ct. R. 33.1(g)')}
    if court in _WORD_CAPS:
        cap, rule_cite = _WORD_CAPS[court]
        # count words in the substantive body: drop markdown tables, the DEFINITIONS/footnote block,
        # and headings-only markers; this over-counts slightly (conservative for a compliance certificate).
        _body = md.split("DEFINITIONS & AUTHORITIES", 1)[0]
        _body = "\n".join(l for l in _body.splitlines() if not l.lstrip().startswith("|"))
        _wc = len(re.findall(r"\b[\w'-]+\b", _body))
        _status = "WITHIN" if _wc <= cap else "OVER"
        if _wc > cap:
            print(f"  [length] *** OVER {rule_cite}: {_wc} words > {cap} cap — TRIM before filing ***")
        else:
            print(f"  [length] {rule_cite}: {_wc} words (cap {cap}) — within limit.")
        _cert = (f"\n\n---\n\n## CERTIFICATE OF COMPLIANCE WITH WORD-COUNT LIMIT\n\n"
                 f"I certify, pursuant to {rule_cite}, that this brief contains {_wc} words, "
                 f"excluding the parts exempted by rule, as counted by the word-processing system used to "
                 f"prepare it, and therefore {'complies with' if _wc <= cap else 'is being conformed to'} "
                 f"the {cap:,}-word limit.\n\n"
                 f"Dated: __________________          _______________________________\n"
                 f"                                    FILER NAME, Appellant, sui juris\n")
        # place the certificate BEFORE the DEFINITIONS/footnote block if present, else append
        if "DEFINITIONS & AUTHORITIES" in md:
            _head, _foot = md.split("---\nDEFINITIONS & AUTHORITIES", 1) if "---\nDEFINITIONS & AUTHORITIES" in md else (md, "")
            md = (_head + _cert + ("\n\n---\nDEFINITIONS & AUTHORITIES" + _foot if _foot else ""))
        else:
            md = md + _cert
    out_docx = Path(draft_md_path).with_name(Path(draft_md_path).stem.replace('_DRAFT','') + '_FILING.docx')
    render_md_to_docx(md, str(out_docx), court_id=court, ctx=ctx if ctx else None)
    print(f"  [docx] wrote {out_docx.name}")
    # PRE-FILE auto-scan (additive; wrapped so it can NEVER block a draft): flags term-of-art misuse in
    # our own draft + surfaces the case's enforcement targets (divergence ledgers) as a sidecar review.
    try:
        from pipeline import pre_file_review
        _rv = pre_file_review.review(md, case_id, draft_path=str(draft_md_path))
        print(f"  [pre-file-review] {_rv['term_of_art_flags']} term-of-art flag(s); "
              f"targets agency={len(_rv['enforcement_targets']['agency'])} court={len(_rv['enforcement_targets']['court'])}")
    except Exception as _e:
        print(f"  [pre-file-review] skipped (non-fatal): {_e}")
    return out_docx


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stub', nargs='?',
                    help='Path to a single stub motion .md')
    ap.add_argument('--case', help='Case ID to process all motions for')
    ap.add_argument('--chain', type=int,
                    help='Limit to chain number (with --case)')
    ap.add_argument('--priority', type=int,
                    help='Limit to motion priority (with --case)')
    ap.add_argument('--batch', help='Process all stubs in given packet dir')
    ap.add_argument('--dry-run', action='store_true',
                    help='Build prompt but do not call LLM')
    ap.add_argument('--force', action='store_true',
                    help='Redraft even when _DRAFT.md is fresher than the stub')
    args = ap.parse_args()

    if args.stub:
        process_stub(args.stub, dry_run=args.dry_run)
        return

    stubs = find_stubs(case_id=args.case, chain=args.chain,
                       priority=args.priority, packet_dir=args.batch)

    # INCREMENTAL_DRAFT_V1 (2026-08-27, user: reanalysis takes too long):
    # skip any stub whose _DRAFT.md already exists and is NEWER than the stub
    # (facts unchanged -> draft still valid). --force overrides. A stub edit
    # (new evidence block, new directive) makes the stub newer and re-queues it.
    if not args.force:
        fresh = []
        for s in stubs:
            d = s.with_name(s.stem + '_DRAFT.md')
            if d.exists() and d.stat().st_mtime > s.stat().st_mtime:
                fresh.append(s)
        if fresh:
            stubs = [s for s in stubs if s not in set(fresh)]
            print(f"[incremental] skipping {len(fresh)} stub(s) with fresh drafts "
                  f"(stub unchanged since last draft); --force to redraft all")
    if not stubs:
        print("No stubs found matching the criteria.")
        print("Try: motion_drafter_v2.py <path/to/stub.md>")
        print("  or: motion_drafter_v2.py --case fed:2:26-cv-00123")
        sys.exit(2)

    print(f"Found {len(stubs)} stub(s) to process.")
    for s in stubs:
        print(f"  - {s}")

    if not args.dry_run:
        print(f"\nThis will call the LLM {len(stubs)} times. "
              f"Continue? (y/N): ", end='', flush=True)
        try:
            confirm = sys.stdin.readline().strip().lower()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(1)
        if confirm not in ('y', 'yes'):
            print("Aborted.")
            sys.exit(0)

    for i, stub in enumerate(stubs, 1):
        print(f"\n[{i}/{len(stubs)}]", end=' ')
        process_stub(stub, dry_run=args.dry_run)


# === DRAFT_WRITE_HOOK_V1 (Batch 5d v2, 2026-06-13) ========================
# Centralizes draft file writes through the post-LLM sanitizer. Closes the
# gap where the Path.write_text monkey-patch from POST_LLM_SANITIZER_V1
# missed plain open()/write() call sites.

def _write_draft_safe(_dwh_path, _dwh_text, _dwh_encoding='utf-8'):
    """Write LLM draft output to disk with post-LLM sanitization applied.

    Sanitization is no-op for non-draft paths. Recognizes both federal
    and PA-CCP (state court) drafts and applies the appropriate ECF/Doc
    label rewrite.
    """
    try:
        import pathlib as _dwh_pl
        p = _dwh_pl.Path(_dwh_path)
        sp = str(p).replace('\\', '/')
        if (isinstance(_dwh_text, str)
                and sp.endswith('_DRAFT.md')
                and '/drafts/' in sp):
            is_pa_ccp = 'phila_ccp' in sp
            new_text, hits = _sanitize_draft_text(_dwh_text, is_pa_ccp=is_pa_ccp)
            if hits:
                try:
                    import sys as _dwh_sys
                    print(f'[draft_write_hook] {p.name} sanitized hits={hits}',
                          file=_dwh_sys.stderr)
                except Exception:
                    pass
            _dwh_text = new_text
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dwh_text, encoding=_dwh_encoding)
        return True
    except Exception as e:
        try:
            import sys as _dwh_sys
            print(f'[draft_write_hook] write failed {_dwh_path}: {e}',
                  file=_dwh_sys.stderr)
        except Exception:
            pass
        return False
# === END DRAFT_WRITE_HOOK_V1 =============================================




# ============================================================================
# BEEZLE_V2_PRIMA_FACIE_V1
# ============================================================================
# Additive merge from now-retired generation/motion_drafter_from_detections.py.
# Injects prima facie detection evidence into v2's LLM prompts. Wraps three
# v2 entry points at module level; touches no existing v2 code.

import re as _pf_re
import glob as _pf_glob
import json as _pf_json
from pathlib import Path as _pf_Path

_PF_REPO_V1 = _pf_Path(str(_eroot()))

# Sections that get the prima facie block (others stay untouched to avoid bloat)
_PF_INJECT_SECTIONS_V1 = frozenset({
    'facts', 'argument', 'background', 'discussion',
    'statement_of_facts', 'procedural_history',
})

# Noise patterns ported from retired drafter v1.3/v1.4
_PF_NOISE_PATTERNS_V1 = [
    r'\baccepted\s+for\s+value\b',
    r'\ba4v\b',
    r'redemption\s+theory',
    r'sovereign\s+citizen',
    r'cestui\s+que\s+vie',
    r'irs\s+form\s+1099',
    r'reset\s+and\s+discharge',
    r'memorandum\s+of\s+law\s+integrating',
    r'title\s+page\s+universal\s+affidavit',
    r'description\s+this\s+filing\s+includes',
    r'plaintiff,?\s+sui\s+juris',
    r'babah\s+filer\s+plaintiff',
    r'universal\s+affidavit',
    r'^\s*\d+\s*$',
    r'^\s*-{3,}\s*$',
    r'^\s*[\.,;:!?]+\s*$',
]
_PF_NOISE_RE_V1 = _pf_re.compile(
    '|'.join('(?:' + p + ')' for p in _PF_NOISE_PATTERNS_V1),
    _pf_re.I | _pf_re.M,
)


def _pf_is_noise_v1(quote):
    """True iff the quote is junk and should be dropped before LLM sees it."""
    if not quote:
        return True
    q = str(quote).strip()
    if len(q) < 30:
        return True
    if _PF_NOISE_RE_V1.search(q):
        return True
    return False


def _pf_case_slug_v1(case_id):
    """Convert v2 case_id (with prefix) → filesystem slug.
    fed:2:26-cv-00123 → fed_26-cv-00123
    phila_ccp:260100000 → phila_ccp_260100000
    """
    if not case_id:
        return ''
    c = str(case_id).strip()
    if ':' not in c:
        return c
    prefix, num = c.split(':', 1)
    # Strip docket subdivision like '2:26-cv-00123' → '26-cv-00123'
    if _pf_re.match(r'^[1-9]:', num):
        num = num.split(':', 1)[1]
    return f'{prefix}_{num}'


def _pf_load_latest_detections_v1(case_id):
    """Find newest prima_facie_detections JSON for a case; return clean list."""
    try:
        slug = _pf_case_slug_v1(case_id)
        if not slug:
            return []
        scope_dir = _PF_REPO_V1 / 'obsidian_sync' / 'cases' / slug / 'scorecards'
        pattern = str(scope_dir / 'prima_facie_detections_*.json')
        files = sorted(_pf_glob.glob(pattern))
        if not files:
            return []
        with open(files[-1], 'r', encoding='utf-8') as fh:
            data = _pf_json.load(fh)
        dets = data.get('detections', []) if isinstance(data, dict) else []
        return [d for d in dets
                if isinstance(d, dict)
                and not _pf_is_noise_v1(d.get('quote', ''))]
    except Exception:
        return []


# Filing-type → preferred detection-category ranking (from retired drafter)
_PF_CATEGORY_PRIORITY_V1 = {
    'motion_to_vacate':    ['MISSING_EXHIBIT', 'RECORD_CORRECTION', 'COURT_ORDER_FINDING', 'JUDICIAL_NOTICE_TRIGGER'],
    'rule_60b':            ['MISSING_EXHIBIT', 'RECORD_CORRECTION', 'COURT_ORDER_FINDING', 'VERIFIED_STATEMENT'],
    'rule_60d':            ['COURT_ORDER_FINDING', 'JUDICIAL_NOTICE_TRIGGER', 'RECORD_CORRECTION'],
    'summary_judgment':    ['AGENCY_RECORD', 'VERIFIED_STATEMENT', 'DEFENDANT_SILENCE', 'COURT_RECORD_CITATION'],
    'partial_msj':         ['AGENCY_RECORD', 'VERIFIED_STATEMENT', 'DEFENDANT_SILENCE', 'COURT_RECORD_CITATION'],
    'judicial_notice':     ['AGENCY_RECORD', 'JUDICIAL_NOTICE_TRIGGER', 'COURT_RECORD_CITATION', 'VERIFIED_STATEMENT'],
    'motion_to_remand':    ['COURT_RECORD_CITATION', 'JUDICIAL_NOTICE_TRIGGER', 'ADVERSARY_ADMISSION'],
    'reconsideration':     ['COURT_ORDER_FINDING', 'RECORD_CORRECTION', 'MISSING_EXHIBIT', 'VERIFIED_STATEMENT'],
    'entry_of_default':    ['DEFENDANT_SILENCE', 'COURT_RECORD_CITATION', 'VERIFIED_STATEMENT'],
    'default_judgment':    ['DEFENDANT_SILENCE', 'COURT_RECORD_CITATION', 'VERIFIED_STATEMENT'],
    'compel_discovery':    ['DEFENDANT_SILENCE', 'COURT_RECORD_CITATION'],
    'motion_to_strike':    ['ADVERSARY_ADMISSION', 'COURT_RECORD_CITATION', 'COURT_ORDER_FINDING'],
    'sanctions':           ['DEFENDANT_SILENCE', 'ADVERSARY_ADMISSION', 'COURT_RECORD_CITATION'],
    'disqualification':    ['COURT_ORDER_FINDING', 'JUDICIAL_NOTICE_TRIGGER'],
    'recusal':             ['COURT_ORDER_FINDING', 'JUDICIAL_NOTICE_TRIGGER'],
    'respa_qwr':           ['VERIFIED_STATEMENT', 'DEFENDANT_SILENCE', 'AGENCY_RECORD'],
    'bad_faith':           ['AGENCY_RECORD', 'DEFENDANT_SILENCE', 'VERIFIED_STATEMENT'],
    'judicial_misconduct': ['COURT_ORDER_FINDING', 'JUDICIAL_NOTICE_TRIGGER', 'ADVERSARY_ADMISSION'],
    'fraud_on_court':      ['COURT_ORDER_FINDING', 'RECORD_CORRECTION', 'ADVERSARY_ADMISSION'],
}


def _pf_filing_type_v1(motion_filename):
    """Extract a normalized filing-type token from the stub filename."""
    if not motion_filename:
        return ''
    stem = _pf_Path(str(motion_filename)).stem.lower()
    stem = _pf_re.sub(r'^\d+_', '', stem)
    stem = _pf_re.sub(r'_(draft|filing|grounded|annotated|state)+$', '', stem)
    return stem


def _pf_format_block_v1(detections, motion_filename, max_items=15):
    """Render filtered, prioritized detections as a markdown block for the
    LLM prompt. Consistent vocabulary with v2's existing scanner_corroboration
    framework — phrased as 'ADDITIONAL SCANNER CORROBORATION' so the LLM
    treats it under the same strict citation rules."""
    if not detections:
        return ''

    ft = _pf_filing_type_v1(motion_filename)
    priority = []
    for key, cats in _PF_CATEGORY_PRIORITY_V1.items():
        if key in ft:
            priority = cats
            break

    def _rank(d):
        cat = d.get('category', '')
        if cat in priority:
            return (0, priority.index(cat))
        return (1, cat)

    selected = sorted(detections, key=_rank)[:max_items]
    if not selected:
        return ''

    lines = [
        '',
        '=== ADDITIONAL SCANNER CORROBORATION: PRIMA FACIE EVIDENCE OF RECORD ===',
        '',
        'The following items are scanner-detected evidence extracted directly',
        'from the case docket and exhibits by prima_facie_detection_engine.',
        'They SUPPLEMENT the stub\'s scanner_corroboration data and are governed',
        'by the same strict citation rules in this prompt: cite by source_doc',
        'and Dkt + page; do not paraphrase as if invented. Each item is',
        'admissible under the Federal Rule of Evidence basis listed.',
        '',
    ]
    for i, d in enumerate(selected, 1):
        src = _pf_Path(str(d.get('source_doc', 'unknown'))).name
        cat = d.get('category', '')
        dkt = d.get('dkt', '\u2014')
        page = d.get('page', '\u2014')
        quote = (d.get('quote', '') or '').strip()
        if len(quote) > 280:
            quote = quote[:277] + '...'
        fre = d.get('fre_basis', '') or ''
        lines.append(f'{i}. [{cat}] {src} (Dkt {dkt}, p.{page}): "{quote}"')
        if fre:
            lines.append(f'   Admissible under: {fre}')
        lines.append('')
    lines.append('=== END PRIMA FACIE EVIDENCE ===')
    lines.append('')
    return '\n'.join(lines)


def _pf_extract_case_id_v1(stub_text):
    """Recover canonical case_id (with jurisdiction prefix) from stub text."""
    if not stub_text:
        return ''
    s = str(stub_text)
    # Explicit Case ID field (most reliable)
    m = _pf_re.search(r'\*\*Case[\s\-_]*ID:?\*\*\s*([^\s\n,]+)', s)
    if m:
        return m.group(1).strip().rstrip(',.;')
    # Federal docket pattern
    m = _pf_re.search(r'\b(fed:\d+:\d+-cv-\d+)\b', s)
    if m:
        return m.group(1)
    # Phila CCP pattern
    m = _pf_re.search(r'\b(phila_ccp:\d+)\b', s)
    if m:
        return m.group(1)
    return ''


# -------------------------------------------------------------------- WRAPPERS

# Wrap build_user_prompt (whole-document prompt path, if used)
try:
    _pf_v1_orig_build_user_prompt = build_user_prompt  # noqa: F821
    _PF_HAS_BUILD_USER = True
except NameError:
    _PF_HAS_BUILD_USER = False

if _PF_HAS_BUILD_USER:
    def build_user_prompt(stub_text, motion_filename):  # noqa: F811
        """Wrapped: original v2 prompt + prima facie evidence block."""
        base = _pf_v1_orig_build_user_prompt(stub_text, motion_filename)
        case_id = _pf_extract_case_id_v1(stub_text)
        if not case_id:
            return base
        detections = _pf_load_latest_detections_v1(case_id)
        if not detections:
            return base
        block = _pf_format_block_v1(detections, motion_filename)
        if not block:
            return base
        return base + '\n\n' + block


# Wrap _section_prompt (active per-section path)
try:
    _pf_v1_orig_section_prompt = _section_prompt  # noqa: F821
    _PF_HAS_SECTION = True
except NameError:
    _PF_HAS_SECTION = False

if _PF_HAS_SECTION:
    def _section_prompt(stub_text, motion_filename, section):  # noqa: F811
        """Wrapped: append prima facie block to facts/argument sections only."""
        base = _pf_v1_orig_section_prompt(stub_text, motion_filename, section)
        sec_key = str(section or '').lower().strip()
        # Strip leading "i. " / "ii. " markers if present
        sec_key = _pf_re.sub(r'^[ivxlcdm]+\.\s*', '', sec_key)
        sec_norm = _pf_re.sub(r'[^a-z_]', '_', sec_key).strip('_')
        # Match if any inject-section name appears as substring
        if not any(target in sec_norm for target in _PF_INJECT_SECTIONS_V1):
            return base
        case_id = _pf_extract_case_id_v1(stub_text)
        if not case_id:
            return base
        detections = _pf_load_latest_detections_v1(case_id)
        if not detections:
            return base
        block = _pf_format_block_v1(detections, motion_filename)
        if not block:
            return base
        return base + '\n\n' + block


# Wrap _render_docx_for_draft (post-save docx empty-quote scrub)
def _pf_scrub_docx_v1(docx_path):
    """Remove paragraphs matching empty-quote artifacts from a DOCX in place."""
    try:
        from docx import Document as _PFDocxDoc
    except Exception:
        return 0
    try:
        doc = _PFDocxDoc(str(docx_path))
    except Exception:
        return 0

    scrub_re = _pf_re.compile(r'(:\s*""\s*$|^\s*""\s*\(|^\s*""\s*$)')
    removed = 0

    def _scrub_paras(paras):
        nonlocal removed
        for p in list(paras):
            t = (p.text or '').strip()
            if scrub_re.search(t):
                try:
                    el = p._element
                    el.getparent().remove(el)
                    removed += 1
                except Exception:
                    pass

    _scrub_paras(doc.paragraphs)
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                _scrub_paras(cell.paragraphs)

    if removed:
        try:
            doc.save(str(docx_path))
        except Exception:
            pass
    return removed


try:
    _pf_v1_orig_render_docx = _render_docx_for_draft  # noqa: F821
    _PF_HAS_RENDER = True
except NameError:
    _PF_HAS_RENDER = False

if _PF_HAS_RENDER:
    def _render_docx_for_draft(draft_md_path, case_id):  # noqa: F811
        """Wrapped: original v2 render + post-save empty-quote scrub."""
        result = _pf_v1_orig_render_docx(draft_md_path, case_id)
        try:
            out_path = None
            if isinstance(result, str):
                out_path = result
            elif hasattr(result, '__fspath__'):
                out_path = str(result)
            elif isinstance(result, dict):
                out_path = result.get('out_path') or result.get('docx_path') or result.get('path')
            if (out_path and str(out_path).endswith('.docx')
                    and _pf_Path(out_path).exists()):
                n = _pf_scrub_docx_v1(out_path)
                if n:
                    import sys as _pf_sys
                    print(f'[v2 pf-scrub] removed {n} empty-quote paragraph(s) from {out_path}',
                          file=_pf_sys.stderr)
        except Exception:
            pass
        return result

# === END BEEZLE_V2_PRIMA_FACIE_V1 ===


# === CASE_LEVEL_FIX_V1 (Batch 4, 2026-06-13) ==============================
# Replaces OMNIBUS_FIX_V1. Wraps build_user_prompt to append case-level
# consolidation guidance for ANY filing type in CASE_LEVEL_FILINGS (omnibus,
# motion_to_remand, rule_60b vacatur, appellate filings, judicial-misconduct
# complaints, etc.). The directive: ONE caption, Roman numerals are legal
# grounds (not defendants), case-wide prayer, no per-defendant enumeration.
#
# Companion to the SCOPE_CLAMP_V1 gate in _section_prompt and to the
# _dedup_case_level_stubs filter in find_stubs.

_CASE_LEVEL_SCOPE_GUIDANCE = (
    "\n\n"
    "=== CASE-LEVEL CONSOLIDATION SCOPE ===\n"
    "This is the SINGLE consolidated filing of its type for this entire "
    "case. It addresses every defendant/party/order to which the relief "
    "extends, under ONE unified set of arguments \u2014 not one section "
    "per defendant.\n"
    "\n"
    "Mandatory drafting rules:\n"
    "  1. ONE caption with the case caption as filed (all parties listed "
    "in the action, not a single-defendant subset).\n"
    "  2. Each Roman numeral in ARGUMENT is a distinct LEGAL GROUND \u2014 "
    "NEVER one section per defendant. Defendant-specific facts go WITHIN "
    "the relevant legal-ground section, not as a separate enumeration.\n"
    "  3. The PRAYER FOR RELIEF must list relief that applies case-wide "
    "(e.g., 'remand the action to the Court of Common Pleas', 'vacate "
    "the order as void', 'grant rehearing en banc', 'reinstate the "
    "appeal') \u2014 NOT per-defendant prayers.\n"
    "  4. Do NOT enumerate parties as separate fact-introduction "
    "paragraphs. Where defendants share a role, refer to them "
    "collectively (e.g., 'the credit-bureau defendants' / 'the furnisher "
    "defendants') only where the legal ground turns on that role.\n"
    "  5. NEVER include HTML comments (<!-- ... -->) in the body.\n"
    "  6. Do NOT create multiple filings; this is the only one for this "
    "filing-type slot in this case.\n"
    "=== END CASE-LEVEL CONSOLIDATION SCOPE ===\n"
)

try:
    _case_level_orig_build_user_prompt = build_user_prompt  # capture original
    def build_user_prompt(stub_text, motion_filename):   # type: ignore[no-redef]
        out = _case_level_orig_build_user_prompt(stub_text, motion_filename)
        try:
            if _is_case_level_filing(motion_filename):
                return out + _CASE_LEVEL_SCOPE_GUIDANCE
        except Exception:
            pass
        return out
except NameError:
    # build_user_prompt not defined at module level \u2014 skip wrapper
    pass
# === END CASE_LEVEL_FIX_V1 ================================================


# === SUBSTANCE_V1: definition anchors wrap BOTH prompt builders =============
# Wraps build_user_prompt() and _section_prompt(). Both paths now get the
# controlling-case definition block prepended before LLM call. This closes
# the P4 gap where section-by-section drafting bypassed the wrapper.

try:
    from pipeline._definition_anchors import anchors_for as _SUBST_anchors_for
    from pipeline._definition_anchors import format_anchor_block as _SUBST_format

    def _SUBST_def_grounding(stub_text):
        """DEFINITION-FIRST grounding: feed the case's corpus-cited word-analysis rebuttal notes into
        the drafter PROSE so a term-of-art order (Hall's 'does not allege ... possesses ... property')
        is rebutted on the controlling legal meaning, not just footnoted. Never raises."""
        try:
            _bk = _baked_grounding_for(stub_text)   # B: prefer analysis-baked definition grounding
            if _bk and (_bk.get("definition") or "").strip():
                return _bk["definition"]
            from pipeline.drafter_grounding import get_definition_grounding
            cid = _pf_extract_case_id_v1(stub_text)   # existing stub->case_id helper
            return get_definition_grounding(cid, stub_text) if cid else ""
        except Exception:
            return ""

    def _SUBST_posture(stub_text):
        """FILING-POSTURE grounding: tell the LLM the current phase, the next deadline, what is already
        on the docket (prior phase — don't re-plead), and to write as appellant if on appeal."""
        try:
            from datetime import date as _date
            from pipeline import filing_status as _fstat
            cid = _pf_extract_case_id_v1(stub_text)
            slug = case_id_to_folder(cid) if cid else ""
            return _fstat.posture_block(slug, _date.today().isoformat()) if slug else ""
        except Exception:
            return ""

    def _SUBST_escalation(stub_text, motion_filename):
        """GROUNDED ESCALATION-LADDER grounding (2026-08-13): for an accountability / conduct / regulatory
        filing, feed m0 the grounded ladder for the forum this filing concerns — each level's statutory
        deadline + cite + on_expiry consequence (escalation_ladders.yaml, corpus-confirmed) — so a Rule 18
        petition / JCB complaint / FOIA-exhaustion / regulator complaint cites the RIGHT next forum and its
        real day-clock instead of inventing one. Scoped to accountability vehicles. Input-only; never raises."""
        try:
            if not _is_accountability_vehicle(motion_filename):
                return ""
            from pipeline import escalation_ladders as _esc
            return _esc.grounding_block(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_rule_grounding(stub_text):
        """MANDATORY-RULE-TEXT grounding (2026-07-08): quote the VERBATIM 'shall/waived' text for every
        rule the stub cites (cite-keyed rules_index) so m0 quotes the operative language beside each
        defect instead of paraphrasing — the crux of an un-ignorable filing. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_rule_grounding
            return get_rule_grounding(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_crossstatute(stub_text):
        """CROSS-STATUTE DEFINITION guard (2026-07-09): the wire the Obsidian link-graph exists for — feed
        the statute-specific CONTROLLING definitions for this filing's operative terms + a do-NOT-import
        instruction (a closed 'means' definition does not travel across titles unless cross-referenced), so
        m0 argues the RIGHT definition for THIS statute every draft. Input-only; never raises."""
        try:
            from pipeline.cross_statute_guard import grounding_block
            return grounding_block(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_pj_posture(stub_text):
        """PERSONAL-JURISDICTION POSTURE + STATUS GUARD (2026-10-20): reservation by role, scope-not-withdrawal,
        property-to-person limit, and the audit's detected switches. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_pj_posture_grounding
            return get_pj_posture_grounding(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_defect_authority(stub_text):
        """BOUND-AUTHORITY grounding (2026-07-12): feed the TIGHT per-defect authority the analysis already
        produced (each audit `procedural_violations[]` carries its own rule_citation + verbatim rule_text), so
        the drafter cites each detected defect with ITS operative authority instead of free-associating a
        nearby cite from the stub (the root cause of WRONG_POINT/UNBACKED cites — fixed at the analysis feed,
        not in post-processing). Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_detected_defect_authority
            return get_detected_defect_authority(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_spotted_levers(stub_text):
        """DEPTH ISSUE-SPOTTER grounding (2026-08-01): feed m0 the VALIDATED levers the depth spotter
        surfaced (audit spotted_levers[]) and the gate confirmed (real cite — BINDING/verbatim/corpus), so
        m0 argues the sharper levers it would otherwise miss, each with its authority. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_spotted_levers_grounding
            cid = _pf_extract_case_id_v1(stub_text)
            return get_spotted_levers_grounding(cid, stub_text) if cid else ""
        except Exception:
            return ""

    def _SUBST_term_cases(stub_text):
        """TERM-OF-ART CASE AUTHORITY (2026-08-01): for each term of art in the draft, feed m0 the cases the
        legal treatises (Black's / CJS / Am Jur) cite defining it — so a term-of-art rebuttal cites the
        controlling case authority the dictionaries rely on, not an invented one. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_term_case_grounding
            return get_term_case_grounding(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_public_law(stub_text):
        """PUBLIC LAW / STATUTES-AT-LARGE grounding (2026-08-02, closes GAP #3): pull the enacting act
        (Pub. L. No. __ / __ Stat. __) for each statute the draft cites, so the drafter can lay the positive-
        law origin under the codified U.S.C. section. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_public_law_grounding
            return get_public_law_grounding(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_maxim_equity(stub_text):
        """MAXIM + EQUITY-PRINCIPLE grounding (2026-08-02, closes GAP #6/#7/#8): pull the actual maxim /
        equity-principle text (Broom's Legal Maxims / Pomeroy's Equity Jurisprudence) from the corpus for the
        operative contexts in the draft, ATTRIBUTED — so a maxim/equity argument cites its treatise source
        instead of a bare Latin string. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_maxim_equity_grounding
            return get_maxim_equity_grounding(stub_text) or ""
        except Exception:
            return ""

    def _SUBST_authority_stack(stub_text):
        """AUTHORITY-STACK LADDER (2026-08-01): for each detected ground (jurisdictional first), feed m0 ONE
        descending ladder — Constitution -> statute verbatim -> BINDING case + HOLDING — so NO section falls
        to bare assertion ('the Court overlooked controlling authority' with nothing named). The real-draft
        test showed the ESTABLISH-DON'T-ARGUE doctrine only fires where the grounding already carried the
        stack; this makes every ground carry it. Resolved by case_id (the scaffold stub has no docket).
        Cached per draft. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_authority_stack_grounding
            cid = _pf_extract_case_id_v1(stub_text)
            return get_authority_stack_grounding(stub_text, case_id=cid or "") or ""
        except Exception:
            return ""

    def _SUBST_court_format(stub_text, motion_filename):
        """FORUM-AWARE format frame (2026-08-02): what THIS trial court requires (FRCP for federal district /
        Pa.R.C.P.+Phila local for Common Pleas) + Am Jur Proof of Facts + the citation ladder, all grounded
        in the corpus. Appellate filings get FRAP 28/Pa.R.A.P. on their own path. Input-only; never raises."""
        try:
            from pipeline.drafter_grounding import get_court_format_grounding
            cid = (_pf_extract_case_id_v1(stub_text) or "").lower()
            mf = str(motion_filename or "").lower()
            is_state = (cid.startswith("phila_ccp") or "ccp" in cid or "pa_super" in cid
                        or "phila" in mf or "_state" in mf)
            return get_court_format_grounding(is_appellate=False, is_state=is_state) or ""
        except Exception:
            return ""

    if "_SUBST_orig_build_user_prompt" not in dir():
        _SUBST_orig_build_user_prompt = build_user_prompt   # type: ignore[has-type]
        def build_user_prompt(stub_text, motion_filename):  # type: ignore[no-redef]
            out = _SUBST_orig_build_user_prompt(stub_text, motion_filename)
            _dg = _SUBST_def_grounding(stub_text)
            if _dg:
                out = _dg.rstrip() + "\n\n" + out
            _rg = _SUBST_rule_grounding(stub_text)
            if _rg:
                out = _rg.rstrip() + "\n\n" + out
            _xs = _SUBST_crossstatute(stub_text)
            if _xs:
                out = _xs.rstrip() + "\n\n" + out
            _pb = _SUBST_posture(stub_text)
            if _pb:
                out = _pb.rstrip() + "\n\n" + out
            _pj = _SUBST_pj_posture(stub_text)          # PJ reservation + status guard — every section
            if _pj:
                out = _pj.rstrip() + "\n\n" + out
            _es = _SUBST_escalation(stub_text, motion_filename)   # grounded escalation ladder (accountability filings)
            if _es:
                out = _es.rstrip() + "\n\n" + out
            _da = _SUBST_defect_authority(stub_text)   # bound DEFECT->authority menu, first in the prompt
            if _da:
                out = _da.rstrip() + "\n\n" + out
            _sl = _SUBST_spotted_levers(stub_text)     # depth issue-spotter's validated levers
            if _sl:
                out = _sl.rstrip() + "\n\n" + out
            _tc = _SUBST_term_cases(stub_text)         # treatise-cited cases for the draft's terms of art
            if _tc:
                out = _tc.rstrip() + "\n\n" + out
            _pl = _SUBST_public_law(stub_text)         # enacting Public Law / Statutes-at-Large per statute
            if _pl:
                out = _pl.rstrip() + "\n\n" + out
            _me = _SUBST_maxim_equity(stub_text)       # maxim/equity principle text (Broom/Pomeroy), attributed
            if _me:
                out = _me.rstrip() + "\n\n" + out
            _as = _SUBST_authority_stack(stub_text)    # per-ground ladder (Const->statute->holding) — topmost skeleton
            if _as:
                out = _as.rstrip() + "\n\n" + out
            try:
                groups = _SUBST_anchors_for(motion_filename, stub_text, section=None)
                if groups:
                    return _SUBST_format(groups) + "\n\n" + out
            except Exception:
                pass
            return out

    if "_SUBST_orig_section_prompt" not in dir():
        _SUBST_orig_section_prompt = _section_prompt   # type: ignore[has-type]
        def _section_prompt(stub_text, motion_filename, section):  # type: ignore[no-redef]
            out = _SUBST_orig_section_prompt(stub_text, motion_filename, section)
            _dg = _SUBST_def_grounding(stub_text)
            if _dg:
                out = _dg.rstrip() + "\n\n" + out
            _rg = _SUBST_rule_grounding(stub_text)
            if _rg:
                out = _rg.rstrip() + "\n\n" + out
            _xs = _SUBST_crossstatute(stub_text)
            if _xs:
                out = _xs.rstrip() + "\n\n" + out
            _pb = _SUBST_posture(stub_text)
            if _pb:
                out = _pb.rstrip() + "\n\n" + out
            _pj = _SUBST_pj_posture(stub_text)          # PJ reservation + status guard — every section
            if _pj:
                out = _pj.rstrip() + "\n\n" + out
            # SPEED (2026-08-02): the argument-building grounding (defect menu, levers, treatise cases, public
            # law, maxim/equity, per-ground ladder) is only used to WRITE THE ARGUMENT — prepending it to the
            # intro/facts/prayer section prompts just bloats the prefill for no benefit. Gate it to the
            # argument section. (build_user_prompt, the whole-doc path, is unaffected.)
            if section == "argument":
                _es = _SUBST_escalation(stub_text, motion_filename)   # grounded escalation ladder (accountability filings)
                if _es:
                    out = _es.rstrip() + "\n\n" + out
                _da = _SUBST_defect_authority(stub_text)   # bound DEFECT->authority menu, first in the prompt
                if _da:
                    out = _da.rstrip() + "\n\n" + out
                _sl = _SUBST_spotted_levers(stub_text)     # depth issue-spotter's validated levers
                if _sl:
                    out = _sl.rstrip() + "\n\n" + out
                _tc = _SUBST_term_cases(stub_text)         # treatise-cited cases for the draft's terms of art
                if _tc:
                    out = _tc.rstrip() + "\n\n" + out
                _pl = _SUBST_public_law(stub_text)         # enacting Public Law / Statutes-at-Large per statute
                if _pl:
                    out = _pl.rstrip() + "\n\n" + out
                _me = _SUBST_maxim_equity(stub_text)       # maxim/equity principle text (Broom/Pomeroy), attributed
                if _me:
                    out = _me.rstrip() + "\n\n" + out
                _as = _SUBST_authority_stack(stub_text)    # per-ground ladder (Const->statute->holding) — topmost skeleton
                if _as:
                    out = _as.rstrip() + "\n\n" + out
                _cf = _SUBST_court_format(stub_text, motion_filename)  # forum frame (FRCP/Pa.R.C.P.) + PoF + ladder — LEADS
                if _cf:
                    out = _cf.rstrip() + "\n\n" + out
            try:
                groups = _SUBST_anchors_for(motion_filename, stub_text, section=section)
                if groups:
                    return _SUBST_format(groups) + "\n\n" + out
            except Exception:
                pass
            return out

except (NameError, ImportError):
    pass
# === end SUBSTANCE_V1 ========================================================



# === PROMPT_CLAMPS_V1 (Batch 5b, 2026-06-13) ==============================
# Two LLM-level prompt clamps appended to every LLM call:
#
#   PLACEHOLDER_BAN (universal): forbid bracketed placeholder tokens
#     that the LLM has been inventing to fill uncertainty gaps after
#     STUB_SANITIZE_V1 (Batch 5a) stripped the stub-level versions.
#
#   PA_CCP_COURT_CLAMP (phila_ccp only): forbid ECF citations in
#     state-court filings; require Pennsylvania Rules of Civil
#     Procedure and state-court citation forms.
#
# Wrapping order on build_user_prompt:
#   PROMPT_CLAMPS_V1  (this layer)
#     -> CASE_LEVEL_FIX_V1  (case-level consolidation guidance)
#       -> original build_user_prompt

_PLACEHOLDER_BAN = (
    "\n\n"
    "=== PLACEHOLDER PROHIBITION (mandatory) ===\n"
    "NEVER emit bracketed placeholder tokens of any kind, including but "
    "not limited to: [record], [date], [citation], [exhibit], [source], "
    "[case name], [case_name], [TODO], <TODO>, [fill in], [insert], "
    "[party], [defendant], [court], [DEFENDANT], or [PLAINTIFF]. If a "
    "specific fact is uncertain, either (a) use the concrete reference "
    "available in the stub (an ECF/docket number, exhibit letter, "
    "named party) or (b) write a complete clause without the "
    "placeholder. NEVER write a sentence containing a bracketed token "
    "waiting to be filled in.\n"
    "\n"
    "Example WRONG: 'Plaintiff\u2019s complaint, [record], establishes ...'\n"
    "Example RIGHT: 'Plaintiff\u2019s complaint (ECF No. 1) establishes ...'\n"
    "Example WRONG: 'As of [date], no answer has been filed.'\n"
    "Example RIGHT: 'No answer has been filed in the period prescribed "
    "by Fed. R. Civ. P. 12(a).'\n"
    "=== END PLACEHOLDER PROHIBITION ===\n"
)

_PA_CCP_COURT_CLAMP = (
    "\n\n"
    "=== PENNSYLVANIA STATE-COURT CONTEXT (mandatory) ===\n"
    "This filing is in the Court of Common Pleas of Philadelphia "
    "County, a Pennsylvania state court. Use ONLY state-court "
    "conventions:\n"
    "  - Cite Pennsylvania Rules of Civil Procedure as "
    "'Pa.R.C.P. <n>'\n"
    "  - Use the docket number and/or control number "
    "(e.g., 'Control No. <n>'), NOT federal ECF citations\n"
    "  - NEVER write 'ECF No. <n>' \u2014 that is federal PACER "
    "nomenclature and does NOT exist in Pennsylvania CCP records\n"
    "  - Refer to the forum as 'this Court' (the Philadelphia CCP), "
    "NOT 'the district court'\n"
    "  - Caption the action as filed in the Court of Common Pleas, "
    "Philadelphia County, with the state-court docket number\n"
    "=== END PENNSYLVANIA STATE-COURT CONTEXT ===\n"
)


def _is_pa_ccp_filing(stub_text):
    """Detect Pennsylvania Court of Common Pleas filing context from
    the stub text. Conservative \u2014 only fires on canonical markers.

    FEDERAL OVERRIDE (2026-07-09): a federal filing (EDPA district or Third
    Circuit appeal) must NEVER take the PA-state clamp, even if the stub
    mentions a Pennsylvania rule in passing (e.g., an authority-hierarchy
    argument that cites Pa.R.C.P. to contrast it with the federal rule).
    Without this guard, a stray 'Court of Common Pleas' / 'Pa.R.C.P.' token in
    an argument block flipped the clamp on for a federal Third Circuit brief and
    the drafter REFUSED ('the mandatory Pennsylvania state-court context
    requires me to draft for the Court of Common Pleas'). Key on unambiguous
    federal-forum caption markers, which never appear in a genuine CCP stub."""
    if not stub_text:
        return False
    t = stub_text.lower()
    federal_forum = (
        "united states district court" in t
        or "united states court of appeals" in t
        or "court of appeals for the third circuit" in t
        or "ecf no." in t
    )
    if federal_forum:
        return False
    return (
        "court of common pleas" in t
        or "phila_ccp" in t
        or "pa.r.c.p." in t
        or "pa.r.civ.p." in t
        or "control no." in t
        or "control number" in t
    )


try:
    _clamps_orig_build_user_prompt = build_user_prompt  # capture upstream
    def build_user_prompt(stub_text, motion_filename):  # type: ignore[no-redef]
        out = _clamps_orig_build_user_prompt(stub_text, motion_filename)
        out = out + _PLACEHOLDER_BAN
        try:
            if _is_pa_ccp_filing(stub_text):
                out = out + _PA_CCP_COURT_CLAMP
        except Exception:
            pass
        return out
except NameError:
    # build_user_prompt not at module level \u2014 skip wrapper
    pass

# The appellate sections draft via _section_prompt (app_case/app_summary/app_argument), which the
# build_user_prompt clamp above does NOT cover \u2014 so a PA appellate brief never got the "use Control No.,
# not ECF" clamp and leaked federal ECF cites. Wrap _section_prompt with the SAME clamps.
try:
    _clamps_orig_section_prompt = _section_prompt   # type: ignore[has-type]
    def _section_prompt(stub_text, motion_filename, section):  # type: ignore[no-redef]
        out = _clamps_orig_section_prompt(stub_text, motion_filename, section)
        out = out + _PLACEHOLDER_BAN
        try:
            if _is_pa_ccp_filing(stub_text):
                out = out + _PA_CCP_COURT_CLAMP
        except Exception:
            pass
        return out
except NameError:
    pass
# === END PROMPT_CLAMPS_V1 ================================================



# === POST_LLM_SANITIZER_V1 (Batch 5d, 2026-06-13) =========================
# Companion to STUB_SANITIZE_V1 (Batch 5a). Where 5a strips placeholders
# from the stub BEFORE the LLM sees them, 5d strips placeholders from the
# LLM's OUTPUT before the draft is written to disk. Catches the cases
# where the LLM ignores PROMPT_CLAMPS_V1's negative instructions.

import re as _post_llm_re

_POST_LLM_RULE_RECORD_WITH_ECF = (
    _post_llm_re.compile(r'\[record,\s*(ECF\s*No\.?\s*\d+(?:[\u2013\u2014\-]\d+)?)\]'),
    r'\1'
)
_POST_LLM_RULE_RECORD_BARE = (
    _post_llm_re.compile(r'\s*\[record(?:,[^\]]*)?\]\s*'),
    ' '
)
# PA state courts cite by "Control No." / docket, never federal "ECF No." Convert a numeric ECF to the
# docket form; convert a placeholder-ECF (e.g. "(ECF No. [VERIFY-NOW])") to a bare "(of record)".
_POST_LLM_RULE_ECF_IN_PA_CCP = (
    _post_llm_re.compile(r'\bECF\s*No\.?\s*(\d+(?:[\u2013\u2014\-]\d+)?)'),
    r'Doc. \1'
)
_POST_LLM_RULE_ECF_PLACEHOLDER_PA = (
    _post_llm_re.compile(r'\s*\(?\s*ECF\s*No\.?\s*\[[^\]]*\]\s*\)?'),
    r' (of record)'
)
_POST_LLM_RULE_GENERIC = [
    (_post_llm_re.compile(r'\s*\[date\]\s*'),           ' '),
    (_post_llm_re.compile(r'\s*\[citation\]\s*'),       ' '),
    (_post_llm_re.compile(r'\s*\[exhibit\]\s*'),        ' '),
    (_post_llm_re.compile(r'\s*\[source\]\s*'),         ' '),
    (_post_llm_re.compile(r'\s*\[case[\s_]?name\]\s*'), ' '),
    (_post_llm_re.compile(r'\s*<TODO[^>]*>\s*'),        ' '),
    (_post_llm_re.compile(r'\s*\[(?:TODO|todo)[^\]]*\]\s*'), ' '),
    (_post_llm_re.compile(r'\s*\[fill\s+in\]\s*'),      ' '),
    (_post_llm_re.compile(r'\s*\[insert\]\s*'),         ' '),
    # catch-all bracketed placeholder the model invents (e.g. [VERIFY-NOW], [VERIFY], [CONFIRM])
    (_post_llm_re.compile(r'\s*\[(?:VERIFY|CONFIRM|CHECK|TBD|PENDING)[^\]]*\]\s*'), ' '),
]
_POST_LLM_RULE_DOUBLE_SPACE       = (_post_llm_re.compile(r'  +'), ' ')
_POST_LLM_RULE_SPACE_BEFORE_PUNCT = (_post_llm_re.compile(r' +([,.;:!?])'), r'\1')
_POST_LLM_RULE_EMPTY_PARENS       = (_post_llm_re.compile(r'\(\s*\)'), '')



# --- DEDUP_REPEATED_PARAGRAPHS_V1 (2026-06-16) -------------------------
# Collapses degenerate LLM repetition loops: consecutive near-identical
# lettered paragraphs (A., B., ... Z.) within a section, and exact-duplicate
# substantive paragraphs. Re-letters survivors sequentially. Skips the
# APPENDIX source stub and fenced code blocks (reference material).
def _dedup_letter(idx):
    s = ""
    idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s

_DEDUP_LETTER_RE = _post_llm_re.compile(r'^([A-Z]{1,2})\.\s+(.*)$')
_DEDUP_HEADING_RE = _post_llm_re.compile(r'^#{1,6}\s')

def _dedup_norm(s):
    s = _post_llm_re.sub(r'^[A-Z]{1,2}\.\s+', '', s)
    s = _post_llm_re.sub(r'\s+', ' ', s).strip().lower()
    return s[:200]

def _dedup_repeated_paragraphs(text):
    lines = text.split('\n')
    out, seen = [], set()
    letter_idx = 0
    frozen = False
    in_code = False
    n_dropped = 0
    for ln in lines:
        stripped = ln.lstrip()
        if frozen:
            out.append(ln); continue
        if stripped.startswith('# APPENDIX'):
            frozen = True; out.append(ln); continue
        if stripped.startswith('```'):
            in_code = not in_code; out.append(ln); continue
        if in_code:
            out.append(ln); continue
        if _DEDUP_HEADING_RE.match(ln):
            letter_idx = 0          # re-letter per section; seen stays global
            out.append(ln); continue
        m = _DEDUP_LETTER_RE.match(ln)
        if m:
            sig = _dedup_norm(ln)
            if len(sig) > 40 and sig in seen:
                n_dropped += 1; continue
            if len(sig) > 40:
                seen.add(sig)
            out.append(_dedup_letter(letter_idx) + '. ' + m.group(2))
            letter_idx += 1
            continue
        sig = _dedup_norm(ln)
        if len(sig) > 80 and sig in seen:
            n_dropped += 1; continue
        if len(sig) > 80:
            seen.add(sig)
        out.append(ln)
    return '\n'.join(out), n_dropped

_SENT_SPLIT_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"'])")
def _dedup_repeated_sentences(text):
    """L8-DEDUP: drop a SUBSTANTIVE sentence (>60 normalized chars) that already appeared verbatim earlier in
    the body — the line-level dedup misses a sentence repeated inside a DIFFERENT paragraph. Keeps the FIRST
    occurrence; preserves headings/lists/tables/code/APPENDIX. Returns (text, n_dropped)."""
    out, seen, n = [], set(), 0
    frozen = in_code = False
    for ln in text.split('\n'):
        s = ln.lstrip()
        if frozen:
            out.append(ln); continue
        if s.startswith('# APPENDIX'):
            frozen = True; out.append(ln); continue
        if s.startswith('```'):
            in_code = not in_code; out.append(ln); continue
        if in_code or s.startswith('#') or re.match(r"^\s*(\d+[.)]\s|[-*]\s|\|)", ln):
            out.append(ln); continue
        kept = []
        for p in _SENT_SPLIT_RE.split(ln):
            norm = _dedup_norm(p)
            if len(norm) > 60 and norm in seen:
                n += 1; continue
            if len(norm) > 60:
                seen.add(norm)
            kept.append(p)
        out.append(' '.join(kept) if kept else '')
    return '\n'.join(out), n

def _drop_empty_sections(text):
    """Remove a heading that has NO body before the next same/higher-level
    heading (e.g. a near-duplicate '### III.' the LLM emitted with no content).
    A heading followed by a SUBheading (more #) is a parent section -> kept.
    Skips the APPENDIX source stub and fenced code blocks. Returns (text, n)."""
    lines = text.split('\n')
    out, n_dropped, i, N = [], 0, 0, len(lines)
    frozen = in_code = False
    while i < N:
        ln = lines[i]; stripped = ln.lstrip()
        if frozen:
            out.append(ln); i += 1; continue
        if stripped.startswith('# APPENDIX'):
            frozen = True; out.append(ln); i += 1; continue
        if stripped.startswith('```'):
            in_code = not in_code; out.append(ln); i += 1; continue
        if in_code:
            out.append(ln); i += 1; continue
        m = _post_llm_re.match(r'^(#{2,6})\s+\S', ln)
        if m:
            L = len(m.group(1)); j = i + 1; empty = True
            while j < N:
                s2 = lines[j]; s2s = s2.lstrip()
                if s2s.startswith('# APPENDIX') or s2s.startswith('```'):
                    break
                hm = _post_llm_re.match(r'^(#{1,6})\s+\S', s2)
                if hm:
                    if len(hm.group(1)) > L:
                        empty = False
                    break
                if s2.strip():
                    empty = False; break
                j += 1
            if empty:
                n_dropped += 1; i += 1; continue
        out.append(ln); i += 1
    return '\n'.join(out), n_dropped
# --- END DEDUP_REPEATED_PARAGRAPHS_V1 ----------------------------------

def _sanitize_draft_text(text, is_pa_ccp=False):
    """Sanitize LLM draft output — the ONE hardening pass, run on every _DRAFT.md write (motion AND
    appellate). Consolidates the leak removal that used to be scattered: reasoning-scratchpad leaks,
    runaway repeats, empty sections, placeholder tokens, and federal ECF cites in PA filings. Returns
    (new_text, hits_dict). (Case-cite verification runs separately in assemble_full_draft because it
    makes rate-limited network calls.)"""
    hits = {}
    # untagged reasoning-model scratchpad ("The user wants...", "Wait, I need to...", "Stub says:")
    _pre = len(text or "")
    text = _strip_reasoning_leak(text)
    if text is not None and len(text) != _pre:
        hits['reasoning_leak_chars'] = _pre - len(text)
    text, _n_dedup = _dedup_repeated_paragraphs(text)
    if _n_dedup:
        hits['repeated_paragraphs'] = _n_dedup
    text, _n_empty = _drop_empty_sections(text)
    if _n_empty:
        hits['empty_sections'] = _n_empty
    new_text, n = _POST_LLM_RULE_RECORD_WITH_ECF[0].subn(
        _POST_LLM_RULE_RECORD_WITH_ECF[1], text)
    if n: hits['placeholder_record_with_ecf'] = n
    text = new_text
    new_text, n = _POST_LLM_RULE_RECORD_BARE[0].subn(
        _POST_LLM_RULE_RECORD_BARE[1], text)
    if n: hits['placeholder_record'] = n
    text = new_text
    if is_pa_ccp:
        new_text, n = _POST_LLM_RULE_ECF_PLACEHOLDER_PA[0].subn(
            _POST_LLM_RULE_ECF_PLACEHOLDER_PA[1], text)   # (ECF No. [VERIFY-NOW]) -> (of record)
        if n: hits['ecf_placeholder_pa'] = n
        text = new_text
        new_text, n = _POST_LLM_RULE_ECF_IN_PA_CCP[0].subn(
            _POST_LLM_RULE_ECF_IN_PA_CCP[1], text)
        if n: hits['ecf_in_pa_ccp'] = n
        text = new_text
    for pat, repl in _POST_LLM_RULE_GENERIC:
        new_text, n = pat.subn(repl, text)
        if n: hits[pat.pattern.strip()] = n
        text = new_text
    text, _ = _POST_LLM_RULE_DOUBLE_SPACE[0].subn(
        _POST_LLM_RULE_DOUBLE_SPACE[1], text)
    text, _ = _POST_LLM_RULE_SPACE_BEFORE_PUNCT[0].subn(
        _POST_LLM_RULE_SPACE_BEFORE_PUNCT[1], text)
    text, _ = _POST_LLM_RULE_EMPTY_PARENS[0].subn(
        _POST_LLM_RULE_EMPTY_PARENS[1], text)
    return text, hits


# Hook this in by wrapping the function that writes draft text to disk.
# In motion_drafter_v2 the most reliable surface is to wrap the final
# write performed inside process_stub. We do this via monkey-patching
# of pathlib.Path.write_text scoped to draft paths during the module's
# operation. Lightweight, no upstream signature changes.
try:
    import pathlib as _post_llm_pathlib
    _post_llm_orig_write_text = _post_llm_pathlib.Path.write_text

    def _post_llm_wrapped_write_text(self, data, *args, **kwargs):
        try:
            sp = str(self)
            if (isinstance(data, str)
                    and sp.endswith('_DRAFT.md')
                    and '/drafts/' in sp.replace('\\', '/')):
                is_pa_ccp = 'phila_ccp' in sp
                new_data, hits = _sanitize_draft_text(data, is_pa_ccp=is_pa_ccp)
                if hits:
                    try:
                        import sys as _post_llm_sys
                        print(f'[post_llm_sanitizer] {self.name} hits={hits}',
                              file=_post_llm_sys.stderr)
                    except Exception:
                        pass
                data = new_data
        except Exception:
            pass
        return _post_llm_orig_write_text(self, data, *args, **kwargs)

    _post_llm_pathlib.Path.write_text = _post_llm_wrapped_write_text
except Exception:
    pass
# === END POST_LLM_SANITIZER_V1 ===========================================



# === DOCKET_INJECTION_V2 (Batch 6, 2026-06-13) =============================
# Builds the "DOCKET HIGHLIGHTS" table that _load_case_audit's prompt already
# tells the LLM to consult. Sources:
#   - obsidian_sync/cases/<slug>/CASE_TIMELINE.json (per case, has docket field)
#   - <home>/_docket_parsed.json (fed_26-cv-00123 PACER enrichment)

import json as _docket_json
import re as _docket_re
from pathlib import Path as _docket_Path

_DOCKET_ROOT = _docket_Path(str(_eroot()))
_DOCKET_PARSED_PATH = _DOCKET_ROOT / '_docket_parsed.json'

def _docket_load_timeline(case_slug):
    p = _DOCKET_ROOT / 'obsidian_sync/cases' / case_slug / 'CASE_TIMELINE.json'
    if not p.exists():
        return []
    try:
        data = _docket_json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    entries = data.get('entries', []) if isinstance(data, dict) else []
    out = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        docket = str(e.get('docket') or '').strip()
        if not docket or docket.lower() in ('none', 'null', 'n/a'):
            continue
        date = str(e.get('date_filed') or '').strip()
        ftype = str(e.get('type') or '').strip()
        filename = str(e.get('filename') or '').strip()
        key = (docket, ftype[:40] if ftype else filename[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append({'docket': docket, 'date': date,
                    'type': ftype, 'filename': filename})
    def _k(e):
        m = _docket_re.search(r'\d+', e['docket'])
        return (int(m.group(0)) if m else 999999, e['date'])
    out.sort(key=_k)
    return out

def _docket_load_parsed():
    if not _DOCKET_PARSED_PATH.exists():
        return {}
    try:
        d = _docket_json.loads(_DOCKET_PARSED_PATH.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _docket_render(entries, parsed=None, is_state=False, max_rows=60):
    if not entries:
        return ''
    label = 'Doc. No.' if is_state else 'ECF No.'
    rows = ['', '## DOCKET HIGHLIGHTS', '',
            f'| {label} | Date | Type | Source |',
            '|---|---|---|---|']
    for e in entries[:max_rows]:
        desc = ''
        if parsed and e['docket'] in parsed:
            d = (parsed[e['docket']].get('desc', '') or '')
            d = d.split('. ')[0][:90].strip()
            desc = d.replace('|', ' ').replace('\n', ' ')
        if not desc:
            fn = (e.get('filename') or '').replace('|', ' ').replace('\n', ' ')
            desc = fn[:90]
        ftype = (e.get('type') or '')[:24].replace('|', ' ')
        rows.append(f"| {e['docket']} | {e['date']} | {ftype} | {desc} |")
    if len(entries) > max_rows:
        rows.append(f"| ... | ... | ... | ({len(entries)-max_rows} more entries) |")
    rows.append('')
    return '\n'.join(rows)

# Wrap _load_case_audit so DOCKET HIGHLIGHTS is always appended.
try:
    _docket_orig_load_case_audit = _load_case_audit  # noqa: F821
    def _load_case_audit(case_id):  # type: ignore[no-redef]
        text = _docket_orig_load_case_audit(case_id) or ''
        try:
            if not case_id:
                return text
            slug = case_id_to_folder(case_id)  # noqa: F821
            is_state = 'phila_ccp' in str(slug)
            entries = _docket_load_timeline(slug)
            enrich = _docket_load_parsed() if slug == 'fed_26-cv-00123' else None
            block = _docket_render(entries, parsed=enrich, is_state=is_state)
            if not block:
                return text
            # For state court, swap "ECF No. N" wording in the audit header.
            if is_state and text:
                text = text.replace("'ECF No. N'", "'Doc. No. N'")
            if not text:
                return ('\n\n---CASE AUDIT (DOCKET HIGHLIGHTS injected; '
                        'use these citation numbers; never write [record])---\n'
                        + block +
                        '\n---END CASE AUDIT---\n')
            if '---END CASE AUDIT---' in text:
                text = text.replace('---END CASE AUDIT---',
                                    block + '\n---END CASE AUDIT---')
            else:
                text = text + '\n' + block
            return text
        except Exception as _docket_e:
            try:
                import sys as _docket_sys
                print(f'[docket_injection] {case_id}: skip ({_docket_e})',
                      file=_docket_sys.stderr)
            except Exception:
                pass
            return text
except NameError:
    pass
# === END DOCKET_INJECTION_V2 ===============================================

# === BEEZLE_PATCH_P3_EMIT_EXHIBITS_V3 ===
# Generates the case scorecard via the existing scorecard_orchestrator.
# render_scorecard_v2 (generation/) produces ONE docx containing BOTH
# Exhibit A (Stare Decisis Hierarchy Scorecard) and Exhibit B (Procedural &
# Civil Rule Violation Scorecard, sections A through K including § G
# Judicial Canon Violations).
#
# Calling the orchestrator more than once per case produces redundant
# identical docx files — the orchestrator's exhibit_label parameter only
# changes the returned judicial_notice_line text, not the docx contents.
#
# One call per case per filing batch. Future per-opposing-reply emission
# will hook on docket-event detection, not on this helper.

def _emit_scorecard(case_id, court_id="EDPA"):
    """Generate the scorecard docx for a case (both exhibits in one file).
    Returns the output path on success, '' on failure. Never raises."""
    try:
        from pipeline.scorecard_orchestrator import build_scorecard_for_case
    except Exception as _e:
        print(f"  [scorecard-emit] orchestrator import failed ({_e})")
        return ""

    if case_id.startswith("phila_ccp") or ":ccp" in case_id.lower():
        court_id = "PHILA_CCP"
    elif case_id.startswith("fed:") or case_id.startswith("fed_"):
        court_id = "EDPA"

    try:
        result = build_scorecard_for_case(
            case_id, court_id=court_id, refresh=True, exhibit_label="A"
        )
        if isinstance(result, dict) and result.get("ok") and result.get("out_path"):
            print(f"  [scorecard-emit] case={case_id} -> {result['out_path']}")
            return str(result["out_path"])
        print(f"  [scorecard-emit] case={case_id} returned: {result!r}")
        return ""
    except Exception as _e:
        print(f"  [scorecard-emit] case={case_id} skipped ({_e})")
        return ""


def _emit_scorecards_for_batch(case_ids):
    """Call _emit_scorecard once per unique case_id."""
    out = {}
    seen = set()
    for cid in case_ids:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out[cid] = _emit_scorecard(cid)
    return out
# === END BEEZLE_PATCH_P3_EMIT_EXHIBITS_V3 ===


# MAIN-GUARD MOVED HERE (2026): later patches appended ~12 defs (_dedup_repeated_paragraphs,
# _sanitize_draft_text, _drop_empty_sections, _docket_*, _emit_scorecard, ...) AFTER the original
# `if __name__` block at ~2796. On a `python -m pipeline.motion_drafter_v2` run, main() fired before
# those defs bound -> "name not defined" for section-drafting + the draft-write hook. Guard must be LAST.
if __name__ == '__main__':
    main()