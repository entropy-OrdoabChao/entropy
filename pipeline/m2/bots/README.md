# m2 bots — one folder each: JOB.yaml (data) · SOUL.md (contract) · CHECK.md (acceptance)

Groups: analysis/ (m0 ANALYSIS, one bot per failure axis) · forum/ · record/ · gate/ · intake/ · ops/ · drafting/
Update a bot = edit its folder. Regenerate JOB.yaml from the spec: `python3 -m pipeline.m2.make_bots`.
Run: `python3 -m pipeline.m2.analysis_runner <draft.md> [--bots a,b] [--claims 1-5] [--workers 6]`

- `analysis/constitution` — unit=claim — Axis 0 — CONSTITUTION: supremacy, due process, PA Art. I §§1/9/11, Art. V; the SCOTUS line binding a state court.
- `analysis/definition_enacted` — unit=term — Axis 1a — DEFINITION (enacted): the statute's own definitions section and the P.L./Stat. text.
- `analysis/definition_dictionary` — unit=term — Axis 1b — DEFINITION (dictionary): Black's edition in force at enactment, Bouvier, Dictionary Act 1 U.S.C. §1.
- `analysis/definition_judicial` — unit=term — Axis 1c — DEFINITION (judicial): how the binding court construed the term.
- `analysis/construction` — unit=term — Axis 1d — CONSTRUCTION: which reading controls when definitions differ (Sutherland, 1 Pa.C.S. §1921–1928, canons).
- `analysis/capacity` — unit=claim — Axis 2 — CAPACITY / PARTY: who really owes the duty, who really holds the right, in what capacity.
- `analysis/element_proof` — unit=count — Axis 3 — ELEMENT / PROOF: every element of every count + the record page that proves it.
- `analysis/procedure` — unit=claim — Axis 4 — PROCEDURE / POSTURE: the SHALL rule at this posture, deadlines, waiver by silence (8(b)(6), 1029(b)), Zerbst symmetry.
- `analysis/authority_binding` — unit=cite — Axis 5a — AUTHORITY (binding?): is this cite binding on THIS court, and still good law?
- `analysis/authority_holding` — unit=cite — Axis 5b — AUTHORITY (what it held): the holding vs what the opponent says it held.
- `analysis/authority_hidden` — unit=claim — Axis 5c — AUTHORITY (hidden, not absent): the controlling case nobody cited; pull + ingest when it is outside the corpus.
- `analysis/equity_maxims` — unit=claim — Axis 6 — EQUITY / MAXIMS: which maxim or equitable rule forecloses discretion here.
- `analysis/remedy` — unit=count — Axis 7 — REMEDY: what the court MUST grant once proved; fees, interest, treble, injunction, 706(2).
- `forum/forum` — unit=claim — FORUM — court × filing type → rule → mandatory attachments/forms/certificates → format → service → judge's procedures.
- `record/trick_miner` — unit=claim — RECORD — TRICK MINER: the opponent's side-step moves, named and countered.
- `record/discretion_closer` — unit=claim — RECORD — DISCRETION CLOSER: the order's departure from text, framed so no discretion remains.
- `record/flip_checker` — unit=claim — RECORD — FLIP CHECKER: every axis turned on OUR draft, as the opponent's best lawyer would.
- `gate/skeptic` — unit=claim — GATE — SKEPTIC: every handoff stamped; nothing invented passes.
- `intake/claim_tracker` — unit=document — INTAKE — CLAIM TRACKER: emails, bills, letters, OCR'd mail → new claims (tort, contract, statutory, regulator) → ledger.
- `ops/case_manager` — unit=case — OPS — CASE MANAGER: the one owner of the case folder; serves slices, records results, runs the clocks.
- `drafting/vehicle_chooser` — unit=case — DRAFTING — which draft wins here: reply / surreply / motion / answer / nothing; posture + forum rule + clock.
- `drafting/answer_or_admit` — unit=claim — DRAFTING — the 8(b)(6)/1029(b) layer: averment-by-averment admit / deny / demand, staccato.
- `drafting/rebuttal` — unit=claim — DRAFTING — per opponent move: waiver-symmetry, substantial-evidence, enacted-authority, binding-order, misstatement.
- `drafting/counts` — unit=count — DRAFTING — one count: elements → dated facts → record cites → the enacted text, in the locked anatomy.
- `drafting/remedies` — unit=count — DRAFTING — the prayer: every mandatory remedy the statute gives, tied to its trigger element.
- `drafting/assembly` — unit=draft — DRAFTING — the locked anatomy: caption, POF Q&A, Schedule A + DEFINITIONS, ABC ENTITIES 1-5, counts, prayer, oath, sui juris/§1308 signature, blank Case No.; then the deterministic guardrails.
