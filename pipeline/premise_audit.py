"""premise_audit — THE PREMISE AUDIT: the question-discipline that beats an adverse filing.

GENUS (user-taught 2026-09-23, derived from his own line of questioning on a PO wave):
every objection an opponent files rests on an UNSTATED PREMISE. Answering the argument
concedes the premise; attacking the premise collapses every argument built on it at once.
The discipline is a fixed sequence of questions asked of ANY adverse filing, in order,
each one refusing to inherit the opponent's framing:

  Q1 WHOSE PAPER IS THIS?        Is it an entered order or a party's request dressed as one?
  Q2 WHAT DID WE ACTUALLY PLEAD? Not what they say we pleaded.
  Q3 WHERE IS THE ENACTED TEXT?  Read the statute before the cases written about it.
  Q4 DOES THE AUTHORITY BIND?    Issuing court vs this forum. Persuasive is not law.
  Q5 DOES THE CASE SAY THAT?     Read the opinion, never their summary of it.
  Q6 IS THE DOCTRINE GENERAL?    A general rule is not an industry rule because they say so.
  Q7 WHAT DOES THE TREATISE SAY? Make them own the gap against the settled statement.
  Q8 WHY THESE COUNTS?           Which remedies does the attack delete?
  Q9 WHAT DID THEY CONCEDE?      What they did NOT raise, and what the rule waives.
  Q10 DID THEY ANSWER OUR SWORN FACTS? Silence on sworn facts is admission, not neutrality.

AGNOSTIC BY CONSTRUCTION: no case names, no statutes, no industries, no party names live
here. Every question is a procedure applied to whatever filing arrives. The questions are
the asset; the answers are per-case.

Worked origin (kept ONLY as provenance, never as matched content): a demurrer asserted the
parties' relationship was "wholly contractual" and that a consumer statute reached only
pre-contract conduct. Q3 ended it — the enacted act's own exclusions clause listed two
exclusions, neither of them the movant's; Q2 showed one objection attacked a theory never
pleaded; Q5 showed two cited cases held something other than what the brief reported.
"""
from __future__ import annotations
import re

# Each question: what it asks, what it FORBIDS (the framing trap), and the artifact it must
# produce in the analysis. Ordered — earlier questions can moot later ones.
PREMISE_QUESTIONS = [
    {
        "id": "Q1_whose_paper",
        "ask": "Who authored this document and what force does it actually carry — an entered "
               "order bearing judicial execution, or a party's requested relief?",
        "forbids": "Treating a proposed order, prayer, or brief assertion as a ruling. A filing "
                   "stamp attests that a PARTY filed it, never that a court granted it.",
        "produces": "Document classification + who must answer it (if anyone).",
    },
    {
        "id": "Q2_our_pleading",
        "ask": "Read OUR operative pleading. Does the theory the movant attacks actually appear "
               "in it — by count and paragraph?",
        "forbids": "Accepting the movant's characterization of our claim, or repleading our "
                   "count into the shape their brief assumes.",
        "produces": "Per-objection: TARGETED (answer it) or MOOT-NO-TARGET (demand they identify "
                    "the count and paragraph; an objection to an unpleaded theory decides nothing).",
    },
    {
        "id": "Q3_enacted_text",
        "ask": "Is there ENACTED TEXT governing this? Pull the statute/rule itself and read its "
               "definitions, its scope, and — decisively — its own EXCLUSIONS/EXCEPTIONS clause.",
        "forbids": "Arguing from cases about a statute before reading the statute. Accepting a "
                   "limitation that the enacted text does not contain.",
        "produces": "Verbatim operative text + whether the movant's asserted limitation appears "
                    "in it. Where the legislature enumerated exceptions and the movant's is not "
                    "among them, expressio unius: no court may add one by construction.",
    },
    {
        "id": "Q4_binding_weight",
        "ask": "For EACH authority cited: (a) which court issued it, and does that court bind THIS "
               "forum on THIS question; (b) is there a LATER decision from a court that binds this "
               "forum that construed, limited, or rejected it — and if so, on the facts as pleaded "
               "in THIS complaint, what did the later binding court hold about the proposition the "
               "movant is advancing, and did the movant's proposition survive there? A cited "
               "case's current holding is the later court's words, not the cited case's original "
               "words.",
        "forbids": "Letting a categorical proposition of law rest on authority that does not bind "
                   "this forum. A court predicting another sovereign's law does not make it. "
                   "Grading a movant's cited case as correctly stated while a later binding court "
                   "construed or limited it — the later construction IS the case's current "
                   "holding, not an aside to footnote.",
        "produces": "Authority ledger (cite | issuing court | binds? | what it is cited for | LATER "
                    "BINDING CONSTRUCTION, if any, with its holding QUOTED) + the controlling "
                    "authority the brief omitted + a SURVIVED / REJECTED verdict on the movant's "
                    "proposition wherever a later construction exists.",
    },
    {
        "id": "Q5_holding_check",
        "ask": "Does the cited case actually hold what the brief says it holds? Read the opinion. "
               "THEN the lawyer's question, verbatim: on the facts as pleaded in THIS complaint, "
               "what did the later binding court hold about the proposition the movant is "
               "advancing, and did the movant's proposition survive there?",
        "forbids": "Characterizing any authority from the opponent's summary of it. Saying "
                   "'correctly states the holding' of a case a later binding court construed or "
                   "limited, without that later holding quoted from corpus. If the opinion is not "
                   "in corpus, mark NOT-VERIFIED and pull it — never infer the holding.",
        "produces": "Per-cite: HOLDS-AS-CITED / STRETCHED / CONTRARY / NOT-VERIFIED, with the "
                    "opinion's own words for anything but the first; and, wherever a later "
                    "binding construction exists, that holding QUOTED with a SURVIVED / REJECTED "
                    "verdict on the movant's proposition.",
    },
    {
        "id": "Q6_doctrine_scope",
        "ask": "Is the doctrine invoked a GENERAL rule of law, or genuinely limited to the "
               "movant's industry, posture, or relationship?",
        "forbids": "Narrowing a general common-law or statutory doctrine to the movant's context "
                   "because the movant framed it that way. Their framing is argument, not a fact "
                   "about the law.",
        "produces": "Scope finding + the general statement of the doctrine with its source.",
    },
    {
        "id": "Q7_treatise",
        "ask": "What is the settled statement of this doctrine in the treatise shelf for THIS "
               "subject, and does the brief cite or distinguish it?",
        "forbids": "Fabricating a treatise section, or hardcoding a volume. Let the SUBJECT of the "
                   "asserted rule pick the volume. Treatises persuade; they do not bind.",
        "produces": "Verbatim treatise statement + the demand: accept the settled statement, or "
                    "state affirmatively that it is wrong and on what authority.",
    },
    {
        "id": "Q8_remedy_targeting",
        "ask": "Which counts were attacked and which were spared — and what does each count's "
               "REMEDY schedule pay?",
        "forbids": "Reading an attack pattern as a merits judgment. Note honestly when a spared "
                   "count is spared because it is hard to attack, not to strip remedies.",
        "produces": "Attacked-vs-spared remedy table; selective attack on multiplier counts is a "
                    "posture finding and an implicit concession that spared counts state claims.",
    },
    {
        "id": "Q9_concessions",
        "ask": "What did this filing NOT raise, and what does the governing rule say about grounds "
               "omitted from it?",
        "forbids": "Overstating waiver. State the waiver rule exactly as written, including its "
                   "own exceptions.",
        "produces": "Waived-grounds list with the rule text and its exceptions; unaddressed facts "
                    "in the record stand unrebutted.",
    },
    {
        "id": "Q10_sworn_facts",
        "ask": ("Did this filing answer our sworn facts? If our verified pleading or any filed "
                "affidavit states sworn facts, did the movant admit them, deny them, or at least "
                "acknowledge them — or did it answer as if they did not exist?"),
        "forbids": ("Treating silence about sworn facts as a neutral act. Under the forum's rules "
                    "silence is itself a legal event: averments in a pleading to which a responsive "
                    "pleading is required are admitted when not denied specifically or by necessary "
                    "implication (Pa.R.C.P. 1029(b), verbatim)."),
        "produces": ("If the case carries sworn facts and the filing does not answer them: "
                     "'SWORN FACTS UNANSWERED — averments admitted under the rule as written' plus "
                     "the unanswered ¶ list. If it answers: a per-¶ admit/deny ledger."),
    },
]


def questions_block() -> str:
    """Render the premise audit as prompt text. Used by the m0 rebuttal task and bot curriculum
    so BOTH read the identical discipline from ONE definition."""
    out = ["PREMISE AUDIT — ask these of the adverse filing IN ORDER before drafting anything. "
           "Every objection rests on an unstated premise; answering the argument concedes the "
           "premise. Record an explicit finding for EACH question (write 'N/A — <why>' when a "
           "question does not reach this filing; never skip silently)."]
    for i, q in enumerate(PREMISE_QUESTIONS, 1):
        out.append(
            f"{i}. [{q['id']}] ASK: {q['ask']} MUST NOT: {q['forbids']} PRODUCE: {q['produces']}")
    out.append("If an earlier question moots a later one (e.g. the paper is not an order, or the "
               "objection has no pleaded target), say so and stop that thread — do not argue the "
               "merits of a premise you have already defeated.")
    return " ".join(out)


_FINDING_RE = re.compile(r"\[(Q\d_[a-z_]+)\]", re.I)


def coverage(analysis_text: str) -> dict:
    """Which premise questions did an analysis actually answer? Deterministic gate so a pass can
    be checked for completeness instead of trusted."""
    found = {m.group(1).upper() for m in _FINDING_RE.finditer(analysis_text or "")}
    want = [q["id"].upper() for q in PREMISE_QUESTIONS]
    missing = [q for q in want if q not in found]
    return {"answered": sorted(found), "missing": missing,
            "complete": not missing,
            "score": f"{len(want) - len(missing)}/{len(want)}"}
