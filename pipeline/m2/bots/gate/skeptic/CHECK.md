# CHECK — skeptic

PASS = stamps clean rows, returns rows with unverified quotes, never stamps a row whose quote the runner marked unverified. Pair: a row with verified quotes (STAMP) vs a row with a plausible but unquoted cite (RETURN).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots skeptic`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.

## ADVERSARY SELF-CONTRADICTION — the second half of the gate (class-level, every case)

The gate does not only grade OUR cards. For every unit the skeptic reads the opposing party's own statements
IN THE RECORD (pleading, letter, notice, verification, certification, cover sheet, docket entry) against each
other and against its own pleading, and posts one card per genuine inconsistency. This is reading for MEANING:
two statements contradict when they cannot both be true of the same subject at the same time. Opposite words
are neither required nor sufficient.

PASS = exactly one card per real pair, NOTHING for statements that are merely different. The pair to test on:

- POSITIVE: the party's pleading classifies the subject one way (the classification that put the case on the
  track it elected and triggered the notices it says it gave) while its own letter/notice/business record for
  the same subject certifies the opposite condition. Expected: ONE card; both statements verbatim; a `[doc:]`
  pointer on EACH; the pleading side labelled JUDICIAL ADMISSION and the other EXTRAJUDICIAL STATEMENT; the
  element it defeats named; a VEHICLE quoted from the pack (or `VEHICLE: NOT IN CORPUS`).
- NEGATIVE: two statements of the party that are consistent once each is read in its own context (the same
  status at two different dates; a general statement and a specific one that fits inside it). Expected: NO
  card. A false contradiction costs the motion; silence costs nothing.

Card form (rides the existing CARD: line — no new parser):

`CARD: skeptic | CONTRADICTION: <admits X vs its own paper states not-X, <= 20 words> | ADMISSION: "<verbatim from the pleading>" [doc:<path>] ; STATEMENT: "<verbatim from its other paper>" [doc:<path>] | ELEMENT: <element defeated> | SUSTAINED | PROPOSITION: <court-facing sentence> VEHICLE: <rule from the pack, or NOT IN CORPUS>`

Deterministic guardrail (`scripts/rooms/cards_to_cells.py`, S-skeptic-contradiction): a skeptic CARD whose
finding starts `CONTRADICTION:` and which carries TWO distinct `[doc:]` pointers becomes a cell with
`kind=contradiction`, `status=RECORD`, `finding` prefixed `CONTRADICTION:` and `record_facts` entries tagged
`source_kind=judicial_admission` / `source_kind=opponent_writing`. One pointer, no pointer, or verdict
REJECTED -> no cell (the claim is not a record fact). The LLM does the comprehension; the parser only keeps
score. `pipeline/corpus_coverage_audit.py` then ticks that unit's OPPONENT_WRITING leg and prints which route
satisfied it.

### The doctrine the classification rests on (corpus, not memory)

- Admissions in pleadings are conclusive on the pleader: "formal judicial admissions which waive any
  evidence by the adverse party on the admitted matter are conclusive on the pleader"
  [corpus:amjur2d_30_evidence#68448b486a] (Am Jur 2d Evidence § 1099).
- A party is bound by its own pleading: "It is an elementary rule that a litigant is not required to prove
  the allegations or facts admitted by his adversary to be true. A party is generally bound by allegations or
  admissions in his own pleadings. This rule applies to all pleadings in the ease, whether a declaration,
  complaint or petition, bill of particulars, plea or answer, or other pleading."
  [corpus:amjur2d_1stPleading_volume_1stpleading#244b159b66] (Am Jur 2d Pleading § 201).
- Federal statement of the same rule: "Judicial admissions are formal admissions in the pleadings which have
  the effect of withdrawing a fact from issue and dispensing wholly with the need for proof of the fact" and
  "Factual assertions in pleadings and pretrial orders, unless amended, are considered judicial admissions
  conclusively binding on the party who made them" [corpus:docvault_pull_f2d_861_224#75bc391e31].
- The other half of the pair is defined: "extrajudicial admission. An admission made outside court
  proceedings." [corpus:frb_chicago_fractional#ca3eeb0471] (Black's, Admission) — evidential and impeaching,
  never conclusive; an admission outside the pleadings "is not conclusive in the civil suit but may be
  explained or rebutted" [corpus:amjur2d_30_evidence#dfebaefa57] (Am Jur 2d Evidence § 1098).
- Vehicle authority is pulled per forum from the pack, never from memory. Held for the Phila. CCP instance:
  preliminary-objection grounds "1028(a)(1), (5) or (6)" with the brief requirement
  [corpus:42-40#40efe3ff11]; "Rule *1028(c). Preliminary Objections. (1) All preliminary objections shall be
  filed with the Office of Judicial Records, as provided in Pa.R.C.P. 1028, together with: (a) a Brief or
  Memorandum of Law" [corpus:phila_cr_1028c_preliminary_objections#a515c37995]. A general denial's effect as
  an admission: [corpus:43-15#d490278334].
- NOT IN CORPUS: a Pennsylvania APPELLATE opinion stating the judicial-admission rule (swept
  amjur2d_30_evidence, amjur2d_1stPleading_volume_1stpleading, Black's, CAP PA reporters, the federal
  docvault line and Pa. Code t231 — the doctrine resolves only to the treatises above plus the federal line).
  Pull a Pa. Super./Pa. opinion on judicial admissions before a brief leans on PA-specific precedent for it.
