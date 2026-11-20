#!/bin/bash
# ORDER_<n> — m2 AS THE ROOM (S-m2-room-roster): NO analysis_runner step. The Hermes hosted room IS m2:
#   room_inputs (record.md limited to the order's counts + per-lane slices) -> ground -> relay (full m2 roster as lanes, waves,
#   per-lane forms, ROOM_LANE_SLICES=1) -> cards_to_cells (gate computes the posture deadline) -> cells_to_stub --write-cf
#   (CASE_FACTS '## M2 LANE FINDINGS') -> OPERATIVE FACTS ledger-consistency assert (STEP 9) -> m1 (invoked exactly once)
# Promoted from ~/.hermes/profiles/main/cache/scratch/chain_order17_m2room.sh (run 48). The order is an ARGUMENT, not a hardcode:
#   usage: chain_order_m2room.sh [TAG]      env: ORDER=ORDER_17 COUNTS=II,III,V,VI BUNDLE=_bundle13_order17 SLUG=... ORDER_OCR=<ocr txt>
#          ORDER_HDR="<adverse.md header line>" POSTURE_SEC="<CASE_FACTS '## POSTURE …' heading>" SECTIONS="A;B" (record.md sections)
#   defaults = ORDER_17 (Phila CCP 260100001, Control No. 26010000, Counts II, III, V, VI dismissed with prejudice).
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
set -u; cd <home>; export PYTHONPATH=.
# S-m1-on-local-gpu (2026-11-16): pipeline LLM = the local-gpu (:8080), locked; the local-gpu is the user's chat lane. m1 / rule_requirements /
# case_facts_refresh default to the retired :18020 container (down since 11-04) → m1 rc=1 "Connection refused" after a GREEN room.
export M1_LLM_URL="${M1_LLM_URL:-http://localhost:8000/v1}"
export BEEZLE_LLAMA_URL="${BEEZLE_LLAMA_URL:-http://localhost:8000/v1/chat/completions}"
SLUG=${SLUG:-example_v_insurer}; C=obsidian_sync/cases/$SLUG
ORDER=${ORDER:-ORDER_17}; COUNTS=${COUNTS-II,III,V,VI};   # L36: UNSET -> the Progressive default; SET-but-EMPTY = no count filter (room_inputs: "" -> None)
 BUNDLE=${BUNDLE:-_bundle13_order17}; B=$C/drafts/$BUNDLE
TAG=${1:-${TAG:-order17m2r49}}; I=$B/room_inputs_m2room; L=$B/m2/chain_m2room.log; PY=/usr/bin/python3
ORDER_OCR=${ORDER_OCR:-$C/intake/_ocr/ccp_order_260100001_notice_of_order_on_case_.pdf.txt}
PLEADING_TXT=${PLEADING_TXT:-$(ls $C/intake/*COMPLAINT*.txt $C/intake/_ocr/*COMPLAINT*.txt $C/intake/*omplaint*.doctext.txt 2>/dev/null | head -1)}   # numbered pleading for the record-closure axis
ORDER_HDR=${ORDER_HDR:-"# $ORDER — Phila CCP No. 260100001, Control No. 26010000, signed 2026-10-21, entered 2026-10-22 (Rule 236 notice), docketed 2026-10-22 ($ORDER_OCR)"}
POSTURE_SEC=${POSTURE_SEC:-"POSTURE UPDATE 2026-10-23 16:00"}
# record.md = the counts the order touched (--counts filter in room_inputs.sections) + the posture update that reports the order.
# ADVERSE-FILING DEFECTS & LEVERS is NOT carried: its AM JUR element chains repeat the Count sections verbatim (run 48: 59 KB → ≤ 15 KB);
# the lanes get their lever material from their slices (ROOM_LANE_SLICES=1, built from the full CASE_FACTS).
SECTIONS=${SECTIONS:-"CAUSES OF ACTION AND ELEMENTS;$POSTURE_SEC"}
SB=${SB:-scripts/rooms}; SBP=scripts/rooms      # SB = relay/room_inputs/cards_to_cells; SBP = promoted ground tools
COURT=${COURT:-PA_CP_PHILA}; FTYPE=${FTYPE:-motion_to_vacate}; CASE_ID=${CASE_ID:-phila_ccp:260100001}
# S-chain-ftype: one chain, any filing type. MSLUG = motion dir (motions/<MSLUG>_m2room), STUB = stub/draft stem; the STAND vehicle +
#   relief come from templates/filing_types/$FTYPE.yaml (vehicle: block), the room brief sentence from VEHICLE_LINE.
MSLUG=${MSLUG:-vacate}; STUB=${STUB:-motion_to_vacate}; M=$B/motions/${MSLUG}_m2room; mkdir -p $I $M $B/m2   # F6c: fresh e2e bundles have no m2/ yet
CELLS=$B/m2/cells_room_m2room.jsonl    # the run-to-closure LEDGER: append + upsert, every round of every run writes here
# ---- S-stance-input (run 53): the amend-vs-stand DECISION is the user's, an input to the chain — never a lane's choice (runs 48-52 let the
#   posture-vehicle/remedy lanes pick 'Motion to Vacate and for Leave to Amend', Rule 1033(a), Relief 3 = First Amended Complaint, Exhibits K/L).
#   STANCE=stand (default): the room brief carries the decision sentence; cells_to_stub --stance stand drops amendment rows/items/exhibits
#   deterministically and writes $M/EXHIBITS_stand.md, which the packet tail reads (PACKET_MANIFEST). STANCE=amend = the pre-run-53 path.
STANCE=${STANCE:-stand}; export STANCE COUNTS
# ---- S-m2-blind-posture (2026-10-27): DERIVE_VEHICLE=1 = the room is NOT told the vehicle. The user's locked input is STANCE
#   (stand/amend) only; WHICH doors exist against this order is the bots' finding from the corpus + vault, ranked, every door
#   (the ones that can run together, the ones that sequence, the ones foreclosed and why). One leg: posture-vehicle, vehicle-foreclosure,
#   appeal-preservation, jurisdiction, void-order, remedy. Output = $B/m2/VEHICLES_<TAG>.md (from the posture/remedy cells); the chain
#   STOPS there — the user picks FTYPE from the bots' list (or re-runs with FTYPE=<pick>). No cells_to_stub, no m1.
if [ "${DERIVE_VEHICLE:-0}" = "1" ]; then
  FTYPE=""; VEHICLE_LINE="vehicle = NOT GIVEN: find EVERY procedural door the written law opens against this order (trial-court motions, finality/certification applications, permissive and as-of-right appeals, preservation steps), each with its verbatim rule, trigger, deadline words, prerequisites, what it wins and what it forecloses; rank them; say which run together and which must sequence."
  CHAIN="posture-vehicle,vehicle-foreclosure,appeal-preservation,jurisdiction,void-order,remedy"
  export ROOM_PROP_FORM_POSTURE="Vehicle <n> of ALL: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Trigger: <event + date from the record>. Deadline rule: <verbatim words>. Prerequisites: <what must exist first>. Wins: <what the court can then do>. Forecloses/sequences: <which other vehicles this one waives, moots, or must precede/follow, with the rule words>. Rank: <1..n> because <reason>. INSTRUMENT: <filing name in snake_case, e.g. preliminary_objections> | CLASS: <pleading|brief|application|affidavit|original_proceeding> | ANATOMY: <sections in order per the forum rule> | RULE THAT MAKES IT THIS CLASS: “<verbatim>” <cite> [corpus:<chunk>] — the INSTRUMENT clause is REQUIRED on every Vehicle card (S-filing-type-on-demand 2026-11-14: m1 routes only from a SUSTAINED instrument card; a vehicle without one cannot be drafted)."
  ROOM_CAPTION=${ROOM_CAPTION:-"$(${PY:-/usr/bin/python3} -c "import json;print(json.load(open('cases_index/case_registry.json'))['progressive']['caption'])" 2>/dev/null) — $ORDER (Control No. ${CONTROL_NO:-}): what are ALL the doors"}
  MSLUG=derive; STUB=vehicles; DERIVE_STOP=1; GP_TYPE=_all
fi
# ---- S-room-caption (BACKLOG 2026-11-13: "ROOM_CAPTION default hardcodes 'Motion to Vacate $ORDER (42 Pa.C.S. § 5505)'"): the
#   default caption named the VEHICLE and the rule, so a blind posture-vehicle lane was handed the answer (and, on a served
#   pleading, was biased into "vacating" an Answer). The default now names the case, the adverse paper and — when the user has
#   picked one — the filing TYPE of record; never a vehicle, never a rule cite. ROOM_CAPTION=<text> still overrides everything.
ROOM_CAPTION=${ROOM_CAPTION:-"$SLUG ($CASE_ID) — $ORDER${CONTROL_NO:+, Control No. $CONTROL_NO}: what does the written law open against this paper, on its own words?${FTYPE:+ (filing type of record: $FTYPE)}"}
export ROOM_CAPTION
STANCE_LINE=""; [ "$STANCE" = "stand" ] && STANCE_LINE=" STANCE (the movant's decision, not yours): ${STANCE_TEXT:-the complaint STANDS as pleaded; no amendment relief, no Rule 1033 vehicle;} ${VEHICLE_LINE:-vehicle = 42 Pa.C.S. § 5505 (modify or rescind within 30 days of entry); relief = vacate $ORDER, reinstate Counts $COUNTS as pleaded, Defendant to answer} in 20 days, and Rule 2.11(A) disqualification/reassignment where the impartiality unit is sustained — Vehicle-form and Relief-form cards state THAT vehicle and THOSE reliefs (a Relief card for leave to amend / an Amended Complaint is discarded)."
# S-card-unit-pin (run 53): cards_to_cells pins a card to its unit by CONTENT against these unit blocks (the lane's 'Issue n' is a hint)
export ROOM_UNITS_MD=<home>/$B/m2/ADVERSE_POINTS.md
# the m2 roster as room lanes (one leg per axis group; skeptic+proof grade every leg, as today). Legs ';', lanes ','.
# leg 1 RULE+FACE: procedural (Axis 4 procedure/posture), verbatim-rule, violation (Axis 4b row), element-proof (Axis 3)
# leg 2 AUTHORITY: authority-stack (binding/hierarchy), authority-holding (Axis 5b what it held), stare-decisis, discretion-closer (RECORD closer)
# leg 3 RELIEF: void-order, remedy, posture-vehicle (vehicle/trigger/rule words — the gate computes the date), jurisdiction
CHAIN=${CHAIN:-"conflict-of-laws,order-silence,demurrer-test,verbatim-rule,procedural,violation,element-proof;jurisdiction,void-order,stare-decisis,authority-stack,authority-holding,appeal-preservation;discretion-closer,dismissal-posture,vacatur-sequence,vehicle-foreclosure,posture-vehicle,remedy"}
LANES=$(echo "$CHAIN" | tr ';' ',')
# F6c (2026-11-11): under tmux dispatch the ROSTER decides the lanes (not CHAIN) and its slice gate requires one slice per roster
# lane — DERIVE mode's 6-lane CHAIN built 6 slices for a 19-lane roster and the room stopped at the gate with 0 lanes launched.
if [ "${ROOM_DISPATCH:-tmux}" = "tmux" ]; then
  ROSTER_LANES=$(${PY:-/usr/bin/python3} -c "import yaml,sys; r=yaml.safe_load(open(sys.argv[1])); print(','.join(dict.fromkeys(l for leg in r.get('legs',[]) for l in leg.get('lanes',[]))))" "${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml}" 2>/dev/null)
  [ -n "$ROSTER_LANES" ] && LANES=$(echo "$LANES,$ROSTER_LANES" | tr ',' '\n' | awk 'NF && !s[$0]++' | paste -sd,)
fi
# ---- ROOM_PROP_FORM (S-mirror-in-prop-form, run 51): the card SHAPE lanes follow is THIS text (relay brief 'PROPOSITION FORM BY LANE'),
#   not the lane's spec skill — run 49/50 agent.log: tool_turns=0 on 33 of 36 lane turns, so asp-<lane>-spec/SKILL.md is never in the
#   prompt; the 3 lanes that happened to skill_view it in run 49 produced 55 of the 57 MIRROR cards. The skeptic reads the same brief
#   and follows its verdict criteria over SOUL (0 NO-MIRROR verdicts in 7 turns with the SOUL rules in its system prompt). Hence MIRROR,
#   the depth floor and the skeptic's NO-MIRROR verdict all live here, once, for the main legs and the re-queue.
PROP_FORM_BASE="ONE violation row in the filing voice: \"Issue <n> — Rule: “<verbatim shall/must words>” <cite> [corpus:<chunk_id>]. Violation: <what $ORDER (${ORDER_DESC:-Control No. 26010000, entered 2026-10-22}) did instead, with the record fact>. QUESTION: <Does <cite> not say “<the same verbatim words>”? <the record fact, dated> — therefore <the consequence the rule's own text compels>.> MIRROR: <≤160-word paragraph: dollars/acts → parties reversed → structural beneficiary → doctrine-vs-count → ${IMPARTIALITY_RULE:-Rule 2.11(A)} question; no motive labels>\" — the MIRROR clause is appended inside the Violation clause of EVERY Issue-form card (a sibling card on the same Issue may write 'MIRROR: as in #<k>', k = your own card number; Vehicle-form and Relief-form cards carry none). DEPTH: one Issue-form card for every numbered point your axis reaches (your slice lists them) — fewer cards than points reached is an incomplete turn. <axis> is your own lane name (never another lane's). skeptic: an Issue-form card with no MIRROR paragraph and no 'MIRROR: as in #<k>' pointer → VERDICT … | OPEN | NO MIRROR; an Issue-form card with no QUESTION clause, or whose QUESTION quotes words not in its own Rule clause → VERDICT … | OPEN | NO QUESTION; 'fraud on the court'/'corrupt'/'bias' asserted as fact → REJECTED | BANNED LABEL. No 'pro se'; the movant is sui juris.$STANCE_LINE"
log(){ echo "[$(date +%T)] $*" | tee -a "$L"; }
T0=$(date +%s)
if [ "${RESUME_FROM:-}" = "m1" ]; then log "RESUME_FROM=m1: pack/rooms/cells/stub reused as on disk ($CELLS, $M/$STUB.md) — drafter + review + packet only"; T3=$(date +%s); else
log "chain start TAG=$TAG ORDER=$ORDER COUNTS=$COUNTS BUNDLE=$BUNDLE STANCE=$STANCE SECTIONS=$SECTIONS"
# ---- 0. turbovec daemon MUST be up in embed mode (2026-10-27: every room run that day grounded on the FTS stand-in —
#   "_retrieve: mode=client_fts — NOT semantically verified" — because nobody started it; posture lane pinned corpus:0).
if ! bash scripts/turbovec_daemon.sh status 2>/dev/null | grep -q '"mode": "embed"'; then
  log "turbovec daemon not in embed mode — starting"
  env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES bash scripts/turbovec_daemon.sh start 2>&1 | tail -1 | tee -a "$L"
  bash scripts/turbovec_daemon.sh status 2>/dev/null | grep -q '"mode": "embed"' || { log "FATAL: turbovec daemon not in embed mode; refusing to run the room on an FTS stand-in"; exit 3; }
fi
# ---- 1. room inputs (header: entered 2026-10-22 Rule 236) + per-lane slices + ground pack
{ echo "$ORDER_HDR"; echo; cat $ORDER_OCR; } > $B/adverse.md
if [ "${DERIVE_VEHICLE:-0}" = "1" ]; then
  # S-m2-blind-posture-r2: the points file is where a vehicle leaks in (ADVERSE_POINTS.md is written around one door).
  # Derive mode builds the points from the ORDER's own operative paragraphs + record facts only — nothing from a draft.
  # >>> DERIVE_UNITS_PY (S-room-units-are-our-theory; tests extract the block between these markers and run it standalone)
  $PY - "$ORDER_OCR" "$C/CASE_FACTS.md" "$B/m2/OUR_ISSUES_${MSLUG}.md" <<'EOF'
import os, re, sys
order = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
facts = open(sys.argv[2], encoding="utf-8", errors="ignore").read() if os.path.exists(sys.argv[2]) else ""
# F6c (2026-11-11): a served PLEADING (Answer + New Matter) derives its issues from the NEW MATTER section only — the Answer's
# hundred 'Denied' paragraphs would otherwise fill the 12-issue cap before the averments that need a response are reached.
# Units are emitted in the ADVERSE_POINTS shape room_inputs.points() parses ('**n. heading**' + 'Record facts:'), and OCR text is
# split on NUMBERED PARAGRAPHS (not blank lines — OCR puts a blank line after every line, so blank-line splitting yields one-liners).
m = re.search(r"(?im)^\s*NEW MATTER\s*$", order)
src = order[m.start():] if m else order
kind = "pleading (NEW MATTER section)" if m else "order"
# S-derive-no-pleading (class, 2026-11-13): when the adverse bundle is NEITHER a pleading NOR a court order (pre-service: docket +
# letters + note copy), slicing it on 'shall/ORDER' yields note boilerplate as units (Planet r1: 40 cells on SCRA notices and
# 'Borrower's promise to pay'). Units are then the user's HYPOTHESIS POINTS (HYPOTHESIS_POINTS=<points.md>, numbered '1. HEADING.'),
# each tested as a hypothesis against the record + written law — never the bundle's own sentences.
hp = os.environ.get("HYPOTHESIS_POINTS", "")
is_order = bool(re.search(r"(?im)^\s*(BY THE COURT|AND NOW,|it is (?:hereby )?ORDERED)", order))
if not m and not is_order and hp and os.path.exists(hp):
    kind = "hypothesis points (no adverse pleading/order on file)"
    ptxt = open(hp, encoding="utf-8", errors="ignore").read()
    paras = [" ".join(x.split()) for x in re.split(r"\n\s*(?=\d{1,2}\.\s+[A-Z])", ptxt) if re.match(r"\s*\d{1,2}\.\s+[A-Z]", x)]
    q = ("Treat this point as a HYPOTHESIS. Is it true on the written law and the record on file? Who/what/when/where; what does the "
         "rule mean by definition; which procedural door does it open in this forum (Rule 1028 ground, affidavit, counterclaim), "
         "and what in the record proves or defeats it? Name gaps NOT IN RECORD / NOT IN CORPUS.")
elif m:
    src = re.split(r"(?im)^\s*(?:WHEREFORE|CERTIFICATE OF SERVICE|VERIFICATION)\b", src)[0]
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*(?=\d{1,3}\.\s)", src) if re.match(r"\s*\d{1,3}\.\s", p)]
    q = ("Is this an averment of FACT that Pa.R.C.P. 1029(b) deems admitted if not denied, or a conclusion of law / recital of a prior "
         "ruling to which no response is required (1029(d))? What does a reply admit, what does silence admit, and which door does the "
         "written law open — without joining the pleader's framing of the dismissed counts?")
else:
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", src) if re.search(r"ORDER|DECREE|sustain|overrul|dismiss|grant|den(y|ied)|strike|shall|prejudice", p, re.I)]
    q = "Which procedural doors does the written law open against THIS act, in this forum, and on what words?"
el = re.search(r"(?s)##\s*(Element chain|ELEMENT CHAIN|Position of record|CAUSES OF ACTION AND ELEMENTS).*?(?=\n## |\Z)", facts)
rec = " ".join(el.group(0).split())[:1800] if el else "(no element chain on file — NOT IN RECORD)"
# ---- S-room-units-are-our-theory (class, 2026-11-13 — BACKLOG "ROOM UNITS = OUR THEORY, NOT THEIR FIRST 12 PARAGRAPHS"):
#   (1) EVERY numbered adverse/hypothesis paragraph is a unit — the old paras[:12] cap dropped New Matter ¶¶120-124 and
#       renumbered the rest, so our own numbering (points.md 1-6, 14-20) never reached a lane or a cell's claim. The unit
#       number IS the source number when the source carries one; the cap is on BYTES (ROOM_UNITS_MAX_BYTES, default 150000)
#       and a drop is announced as 'FORCED' with the unit numbers dropped — never a silent slice.
#   (2) one AFFIRMATIVE unit per COUNT of CASE_FACTS '## CAUSES OF ACTION AND ELEMENTS' (COUNTS filter when set), numbered
#       after the adverse units — our count theory reaches the lanes as its own unit, not as an 1,800-char record tail.
CAP = int(os.environ.get("ROOM_UNITS_MAX_BYTES", "150000"))
lab = "hypothesis" if kind.startswith("hypothesis") else ("pleading" if m else "order")
numbered = []
for i, p in enumerate(paras, 1):
    mn = re.match(r"\s*(\d{1,3})\.\s", p)
    numbered.append(((int(mn.group(1)) if mn else i), p))
seen, units = set(), []
for n, p in numbered:
    while n in seen: n += 1                       # a duplicate source number never collapses two paragraphs into one unit
    seen.add(n)
    head = p[:140].rstrip(" ,;:")
    body = (f"THE HYPOTHESIS (defendant's point to test): \"{p[:700]}\"" if lab == "hypothesis"
            else f"THE PAPER UNDER REVIEW (adverse, verbatim): \"{p[:700]}\"")
    units.append((n, f"**{n}. The {lab} states: {head}**\n\n{body}\n\nQuestion: {q}\n\nRecord facts: {rec}\n"))
# affirmative COUNT units (our own theory), one per count, after the adverse numbering
counts_want = [c.strip().upper() for c in (os.environ.get("COUNTS") or "").split(",") if c.strip()]
cu, nxt = [], (max(seen) + 1 if seen else 1)
if el:
    sec = el.group(0)
    heads = list(re.finditer(r"(?m)^#{2,4}\s*Count\s+([IVXLC]+|\d+)\s*[:：—–-]?\s*(.*)$", sec))
    for j, h in enumerate(heads):
        roman = h.group(1).upper()
        if counts_want and roman not in counts_want: continue
        cbody = " ".join(sec[h.end(): heads[j + 1].start() if j + 1 < len(heads) else len(sec)].split())[:1400]
        title = (h.group(2).strip() or f"Count {roman}")[:120]
        cu.append((nxt, f"**{nxt}. OUR COUNT {roman} — {title} (affirmative unit: our pleaded theory, not their paper)**\n\n"
                        f"OUR CAUSE OF ACTION (CASE_FACTS '## CAUSES OF ACTION AND ELEMENTS', verbatim): \"{cbody}\"\n\n"
                        f"Question: element by element, does the record on file prove THIS count (every element, with the record fact and the "
                        f"verbatim rule/holding that supplies it)? Which element is unproved and what document would prove it? "
                        f"Name gaps NOT IN RECORD / NOT IN CORPUS.\n\nRecord facts: {rec}\n"))
        nxt += 1
# ---- CHAIN-OF-TITLE / CAPACITY canon (class, 2026-11-14 — "I want this filing to be airtight" / "how can
#   m2bots/room use this kind of thing in other cases?"): capacity used to reach the room as ONE hand-written
#   point, so the links BETWEEN the originator and today's claimant were never tested one at a time. The canon
#   is now a STANDING DERIVE QUESTION (scripts/rooms/chain_of_title.QUESTION, family-agnostic) plus one unit
#   PER LINK, derived by the comprehension lane from CASE_FACTS + the record + the case's own document
#   inventory (and the connected cases its facts link to). The guard is DETERMINISTIC: a link unit must carry
#   >= 1 [doc:] that exists in the record AND >= 1 rule cite that pipeline.cite_family header-pins in the
#   corpus; a link that fails either test is emitted as status OPEN with PULL: lines — NEVER dropped, because a
#   missing link IS the defect being filed. Fail-soft: any error leaves the adverse/count units untouched.
#   CHAIN_CANON=0 disables; CHAIN_LLM_FIXTURE=<json> replays a recorded answer for dry runs.
ct = []
if os.environ.get("CHAIN_CANON", "1") != "0":
    try:
        import json as _json, importlib.util as _ilu
        _root = os.environ.get("BEEZLE_CODE_ROOT", os.path.expanduser("~/beezle"))
        _mp = os.environ.get("CHAIN_OF_TITLE_PY", os.path.join(_root, "scripts", "rooms", "chain_of_title.py"))
        _spec = _ilu.spec_from_file_location("chain_of_title", _mp)   # scripts/ is not a package: load by path
        _ct = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_ct)
        _facts_p = sys.argv[2]
        _roots = [x for x in (os.environ.get("CHAIN_DOC_ROOTS", "").split(os.pathsep)) if x] or \
                 _ct.connected_roots(facts, os.path.dirname(os.path.abspath(_facts_p)))
        _inv = _ct.doc_inventory(facts, "", _roots)
        _d = _ct.derive_links(facts, "", _inv)
        _con = None
        try:
            _con = _ct._db()
        except Exception as _e:
            print(f"derive chain-of-title: corpus unavailable ({type(_e).__name__}) — rule cites emitted as PULL:")
        _md, _side = _ct.units(_d, _inv, start_n=nxt, con=_con, corpus=(_con is not None), record_tail=rec)
        if _con:
            _con.close()
        if _md.strip():
            ct = [(nxt, _md)]
            nxt += len(_side["units"])
        _sc = os.path.join(os.path.dirname(os.path.abspath(sys.argv[3])), "CHAIN_OF_TITLE.json")
        open(_sc, "w", encoding="utf-8").write(_json.dumps(_side, indent=2, ensure_ascii=False) + "\n")
        print(f"derive chain-of-title: {len(_side['units'])} link unit(s) (OPEN {_side['open_units']}; "
              f"families {_side['families']}; inventory {len(_inv)} docs from {len(_roots)} case dir(s)) -> {_sc}")
    except Exception as e:
        print(f"derive chain-of-title: SKIPPED ({type(e).__name__}: {str(e)[:140]}) — adverse/count units unchanged")
out = [f"# ISSUES — derived from the {kind} itself (DERIVE_VEHICLE; no vehicle given)", ""]
kept, dropped, used = [], [], 0
for n, blk in units + cu + ct:
    b = len(blk.encode("utf-8"))
    if used + b > CAP and kept:
        dropped.append(n); continue
    kept.append((n, blk)); used += b
out += [blk for _, blk in kept]
open(sys.argv[3], "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"derive points: {len(kept)} units from the {kind} (numbers preserved: {[n for n, _ in kept]}; "
      f"adverse={len(units)} affirmative-count={len(cu)}; {used}B of {CAP}B cap)")
if dropped:
    print(f"derive points: FORCED byte cap {CAP}B — unit(s) {dropped} dropped; raise ROOM_UNITS_MAX_BYTES or split the room")
EOF
  # <<< DERIVE_UNITS_PY
elif [ -s "$B/m2/ADVERSE_POINTS.md" ]; then
  sed "s/^PLAINTIFF'S POSITION OF RECORD (CASE_FACTS element chain, verbatim): */Record facts: /; s/^PLAINTIFF'S POSITION OF RECORD[^:]*: */Record facts: /" $B/m2/ADVERSE_POINTS.md > $B/m2/OUR_ISSUES_${MSLUG}.md
elif [ -s "$B/m2/OUR_ISSUES_derive.md" ]; then
  # FTYPE pass on a ledger CLOSED in DERIVE mode (no ADVERSE_POINTS.md in this case): the units of record ARE the derived units —
  # the same file the cells were pinned to. Rebuilding from a missing points file produced an empty units file (2026-11-14 16:18).
  cp -p $B/m2/OUR_ISSUES_derive.md $B/m2/OUR_ISSUES_${MSLUG}.md; log "units: reusing derive-mode units ($B/m2/OUR_ISSUES_derive.md) for MSLUG=$MSLUG"
fi
# ---- S-room-closure / S-room-units-are-our-theory: ONE units file of record for this run — ADVERSE_POINTS.md when the case has
#   one, else the derived OUR_ISSUES_<MSLUG>.md (derive mode had NO units file, so ROOM_UNITS_MD pointed at a missing path and the
#   unit pin fell back to verified_record.md). A room is never opened without a points file (ROOM_NO_POINTS=1 to override).
UNITS_MD=$B/m2/ADVERSE_POINTS.md; [ -s "$UNITS_MD" ] || UNITS_MD=$B/m2/OUR_ISSUES_${MSLUG}.md
export ROOM_UNITS_MD=<home>/$UNITS_MD
U_N=$(grep -cE '^\*\*[0-9]+\. ' "$UNITS_MD" 2>/dev/null || true); U_N=${U_N:-0}
U_LIST=$(grep -oE '^\*\*[0-9]+\.' "$UNITS_MD" 2>/dev/null | tr -d '*.' | paste -sd, || true)
if [ "$U_N" -lt 1 ]; then
  if [ "${ROOM_NO_POINTS:-0}" = "1" ]; then log "WARN: no units in $UNITS_MD — ROOM_NO_POINTS=1: the room opens with NO units layer (no lane gets a point)"
  else log "FATAL: no units in $UNITS_MD — refusing to open a room without a points file (set ROOM_NO_POINTS=1 to override)"; exit 4; fi
fi
log "units: $U_N unit(s) [$U_LIST] from $UNITS_MD (ROOM_UNITS_MD=$ROOM_UNITS_MD)"
RB=$([ -f $I/record.md ] && stat -c %s $I/record.md || echo 0)
# ---- 1z. room_inputs RECORD cache (S-analysis-memo (6), the shape of the ground_pack stamp): record.md (+ the resolved
#   connected_record.md inside it) is a pure function of adverse.md + the units file + CASE_FACTS + SECTIONS/COUNTS +
#   room_inputs.py + connected_record.py. HIT = skip the build, logged with the sha; MISS = rebuild by the SAME command + restamp.
RI_STAMP=$I/room_inputs.sha256
RI_SHA=$( { cat $B/adverse.md $B/m2/OUR_ISSUES_${MSLUG}.md $C/CASE_FACTS.md $SB/room_inputs.py $SB/connected_record.py 2>/dev/null; echo "$SECTIONS|$COUNTS"; } | sha256sum | cut -c1-64)
if [ -s $I/record.md ] && [ "$(cat $RI_STAMP 2>/dev/null)" = "$RI_SHA" ] && [ "${ROOM_FORCE_INPUTS:-0}" != "1" ]; then
  log "room_inputs record cache HIT sha=${RI_SHA:0:12} ($RB B, built $(stat -c %y $I/record.md | cut -c1-19)) — room_inputs.py record build skipped"
else
  log "room_inputs record cache MISS (stamp=$(cat $RI_STAMP 2>/dev/null | cut -c1-12 || echo none) want=${RI_SHA:0:12}) — rebuilding"
  timeout 600 $PY $SB/room_inputs.py --adverse $B/adverse.md --points $B/m2/OUR_ISSUES_${MSLUG}.md --facts $C/CASE_FACTS.md \
    --sections "$SECTIONS" --counts "$COUNTS" --out $I 2>&1 | tail -2 | tee -a "$L"
  [ -s $I/record.md ] && echo "$RI_SHA" > $RI_STAMP
  log "record.md bytes before=$RB after=$(stat -c %s $I/record.md) (counts $COUNTS); stamp ${RI_SHA:0:12} written"
fi
# ---- 1m. ANALYSIS MEMO (BACKLOG 2026-11-13 "ROOM — never re-analyze what the ledger already closed; carry it as CONTEXT
#   only"). THE LEDGER DECIDES WHAT THIS ROOM WORKS, before anything is built: scripts/rooms/analysis_memo.py reads the
#   run-to-closure ledger ($CELLS, append+upsert) through the SAME coverage rule cells_to_stub enforces, computes an inputs-sha
#   per unit (unit text ∥ the record/adverse lines its cells cite ∥ the authority keys its cells cite) and writes
#   $B/m2/ANALYSIS_MEMO.json: {unit: status, inputs_sha, closed_by_run, context_block}.
#     M=0 (every unit CLOSED on unchanged inputs) -> the room is NOT opened: prefetch, pack, slices, lanes and graders are all
#       skipped and the chain goes straight to VEHICLES/derive (or the stub/m1 tail) — the old behaviour re-reasoned the whole
#       set every round (r4: 58 min for material already GROUNDED).
#     M>0 -> ROOM_UNITS is set to the OPEN set automatically (a ROOM_UNITS already in the env still wins), and the CLOSED units
#       ride into every producer prompt as ≤ 300 B CONTEXT blocks (dispatch_prompt) with prior cards as pointers (relay_priors).
#   Knobs: ROOM_ANALYSIS_MEMO=0 (off, pre-memo behaviour), ROOM_FORCE_UNITS=<n,…>, ROOM_FORCE_ALL=1.
MEMO=$B/m2/ANALYSIS_MEMO.json; export ROOM_MEMO=<home>/$MEMO
MEMO_SKIP=0; OPEN=""
if [ "${ROOM_ANALYSIS_MEMO:-1}" = "1" ]; then
  MG0=$(date +%s)
  MEMO_OUT=$(env -u PYTHONPATH timeout 600 $PY $SB/analysis_memo.py --gate --units "$UNITS_MD" --cells "$CELLS" \
    --inputs $I --memo $MEMO --cov $B/m2/COVERAGE.md --cards-dir output/research/rooms_ab --round 0 2>/dev/null | tail -2)
  MEMO_LINE=$(echo "$MEMO_OUT" | grep -m1 '^analysis memo:' || true)
  MEMO_OPEN=$(echo "$MEMO_OUT" | grep -m1 '^OPEN=' | sed 's/^OPEN=//' || true)
  log "${MEMO_LINE:-analysis memo: UNAVAILABLE (gate failed) — room opens on every unit} -> $MEMO (gate wall $(( $(date +%s) - MG0 ))s)"
  if [ -n "${MEMO_LINE:-}" ] && [ -z "${MEMO_OPEN:-}" ]; then
    MEMO_SKIP=1
    log "ledger CLOSED — room not opened: prefetch, ground_pack, lane slices, the room and the graders are SKIPPED (every unit carries a sustained card from a lane that exited 0, on inputs whose sha has not changed). ROOM_FORCE_UNITS=<n,…> or ROOM_FORCE_ALL=1 to re-open."
  elif [ -n "${MEMO_OPEN:-}" ]; then
    ROOM_UNITS=${ROOM_UNITS:-$MEMO_OPEN}; export ROOM_UNITS
    log "analysis memo: ROOM_UNITS=$ROOM_UNITS (the OPEN set; a ROOM_UNITS already in the env overrides)"
  fi
else
  log "analysis memo: OFF (ROOM_ANALYSIS_MEMO=0) — every unit is worked, nothing is carried as CONTEXT"
fi
if [ "$MEMO_SKIP" != "1" ]; then

# F7 (2026-11-11): RECHARACTERIZATION WATCHER — their paragraph beside our pleaded paragraph per count (deterministic guardrail);
# the characterization lane fills the RECHARACTERIZATION field of its card from it; the skeptic sees the same table.
if [ -s "$PLEADING_TXT" ]; then
  $PY $SB/recharacterization_table.py $B/adverse.md "$PLEADING_TXT" $I/recharacterization_table.md 2>&1 | tail -1 | tee -a "$L"
else
  log "recharacterization_table skipped: no numbered pleading text (PLEADING_TXT empty)"
fi
# ---- 1a. ground_pack cache (S-ground-pack-cache, run 50): the pack is a pure function of adverse.md + verified_record.md +
#   forum_rules.json + ground_pack.py (≈ 7 min CPU). sha256 of those four, stored beside the pack in $I/ground_pack.sha256;
#   a matching stamp with the pack on disk → skip (HIT); anything else → rebuild by the SAME command (byte-identical path) + restamp.
GP_STAMP=$I/ground_pack.sha256
# S-room-depth: + the unit file (the pack now resolves OUR units' authorities) + a corpus stamp (size+mtime of chunk_text.db) —
#   a pack cached before an ingest kept serving the old corpus (Cintas / State Equipment pulled 10-28, never reached the room).
GP_POINTS=$B/m2/OUR_ISSUES_${MSLUG}.md
GP_CORPUS=$(stat -c '%s %Y' ~/turbovec/chunk_text.db 2>/dev/null)
GP_SHA=$( { cat $I/adverse.md $I/verified_record.md $SBP/forum_rules.json $SBP/ground_pack.py $SBP/rebuttal_table.py $GP_POINTS $ORDER_OCR $PLEADING_TXT 2>/dev/null; echo "$GP_CORPUS ${GP_REBUTTAL:-1}"; } | sha256sum | cut -c1-64)
TG0=$(date +%s)
if [ -s $I/ground_pack.md ] && [ "$(cat $GP_STAMP 2>/dev/null)" = "$GP_SHA" ]; then
  log "ground_pack cache HIT sha=${GP_SHA:0:12} ($(stat -c %s $I/ground_pack.md) B, built $(stat -c %y $I/ground_pack.md | cut -c1-19)) — ground_pack.py skipped"
else
  log "ground_pack cache MISS (stamp=$(cat $GP_STAMP 2>/dev/null | cut -c1-12 || echo none) want=${GP_SHA:0:12}) — rebuilding"
  # S-rebuttal-table (2026-11-01): REBUTTAL ROWS + axes ON by default (GP_REBUTTAL=0 opts out); GP_ORDER = the order under attack,
  #   GP_RECORD = the numbered pleading (RECORD-CLOSURE axis: averment -> answered / silent -> addressed by the order?).
  GP_REBUTTAL=${GP_REBUTTAL:-1} GP_ORDER=${GP_ORDER:-$ORDER_OCR} GP_RECORD=${GP_RECORD:-$PLEADING_TXT} \
  GROUND_PACK_RULE_CHARS=700 timeout 900 $PY $SBP/ground_pack.py $I/adverse.md $I/verified_record.md $I/ground_pack.md \
    --court $COURT --type ${GP_TYPE:-$FTYPE} --case $CASE_ID --points $GP_POINTS 2>&1 | grep -viE "warning|onnx|batches" | tail -3 | tee -a "$L"
  [ -s $I/ground_pack.md ] && echo "$GP_SHA" > $GP_STAMP
  log "ground_pack rebuilt in $(( $(date +%s) - TG0 ))s; stamp ${GP_SHA:0:12} written"
fi
# slices need ground_pack.md + verified_record.md in place -> built after the pack (one ≤ 12 KB file per lane)
CUDA_VISIBLE_DEVICES= timeout 600 $PY $SB/room_inputs.py --facts $C/CASE_FACTS.md --out $I --lane-slices "$LANES" 2>&1 | grep -v Warning | tail -1 | cut -c1-300 | tee -a "$L"
# S-prefetch (2026-11-10): deterministic PRE-ROOM FETCH — every cite named in verified_record/record/adverse (+CASE_FACTS) is resolved
#   BY KEY through pipeline/m2/fetch_bot.resolve BEFORE any lane launches; hits land in $I/ground_pack.md as GROUND+ lines
#   ('## RESOLVED AUTHORITIES …', pasted into every lane's pack CORE by dispatch_prompt), misses as '## NOT RESOLVED …' = the fetch
#   lane's work list. ROOM_PREFETCH=0 disables. ROOM_PREFETCH_PULL defaults to 1 (user-locked 2026-11-10): a TRUE NOT IN CORPUS
#   (every family swept, no pin) triggers fetch_bot's pull ladder — Harvard CAP static FIRST (given cite → parallel cites) → Wikisource →
#   CourtListener LAST — staged under corpus_sources/ for ingest. Case cites only; statute gaps go to the official-source puller.
ROOM_PREFETCH=${ROOM_PREFETCH:-1}
ROOM_PREFETCH_PULL=${ROOM_PREFETCH_PULL:-1}
if [ "$ROOM_PREFETCH" = "1" ]; then
  TP0=$(date +%s)
  # S-analysis-memo (6): PREFETCH CACHE — cites→pins is a pure function of the record layer + the resolver + the corpus.
  #   HIT = skip (the pack already carries its GROUND+ lines), logged with the sha; MISS = resolve + restamp.
  PF_STAMP=$I/prefetch.sha256
  PF_SHA=$( { cat $I/record.md $I/adverse.md $I/verified_record.md $SB/room_prefetch.py 2>/dev/null; echo "$GP_CORPUS $ROOM_PREFETCH_PULL"; } | sha256sum | cut -c1-64)
  # DELTA (2026-11-14): ROOM_FORCE_ALL opens every UNIT; it no longer busts this cache (that cost 270-330 s every run). ROOM_FORCE_PREFETCH=1 does.
  if [ -s $I/prefetch.json ] && [ "$(cat $PF_STAMP 2>/dev/null)" = "$PF_SHA" ] && [ "${ROOM_FORCE_PREFETCH:-0}" != "1" ] && grep -q '^GROUND+:' $I/ground_pack.md 2>/dev/null; then
    log "prefetch cache HIT sha=${PF_SHA:0:12} ($(grep -c '^GROUND+:' $I/ground_pack.md) GROUND+ lines in the pack, resolved $(stat -c %y $I/prefetch.json | cut -c1-19)) — room_prefetch.py skipped"
  else
  log "prefetch cache MISS (stamp=$(cat $PF_STAMP 2>/dev/null | cut -c1-12 || echo none) want=${PF_SHA:0:12}) — resolving"
  env -u PYTHONPATH CUDA_VISIBLE_DEVICES= timeout 900 $PY $SB/room_prefetch.py "$I" $([ "$ROOM_PREFETCH_PULL" = "1" ] && echo --pull) 2>&1 | grep -v -i warning | grep -v '^\[retrieval\]' | tail -1 | cut -c1-300 | tee -a "$L"
  [ -s $I/prefetch.json ] && echo "$PF_SHA" > $PF_STAMP
  log "prefetch done in $(( $(date +%s) - TP0 ))s (ROOM_PREFETCH_PULL=$ROOM_PREFETCH_PULL); GROUND+ lines in pack: $(grep -c '^GROUND+:' $I/ground_pack.md 2>/dev/null || echo 0); stamp ${PF_SHA:0:12} written"
  fi
else
  log "prefetch skipped (ROOM_PREFETCH=0)"
fi
# S-topic-autopull (2026-11-13, user-locked: NOT IN CORPUS => pull automatically, Harvard/CourtListener; never a hand pull):
#   prefetch resolves CITES named in the record; units whose LAW has no cite yet (a definition, an element, a statute the
#   adverse paper never names) pulled nothing, so the rooms carded them without rule text. scripts/rooms/topic_pull.py reads every
#   **n. unit of $B/m2/OUR_ISSUES_*.md, has the LLM name the propositions of law it needs, probes the corpus for BINDING hits, and
#   on a miss pulls forum-court opinions by topic (CourtListener v4, court ids from COURT) + the statute (palegis P.S. act PDF /
#   staged raw) through case_fetch.ingest_case / official_source_puller, then runs the turbovec embed delta. Report:
#   $I/topic_pull.json; GROUND+ lines appended to $I/ground_pack.md. ROOM_TOPIC_PULL=0 disables.
ROOM_TOPIC_PULL=${ROOM_TOPIC_PULL:-1}
if [ "$ROOM_TOPIC_PULL" = "1" ] && [ -n "${RESUME_FROM:-}" ]; then   # resume: the pack was just rebuilt -> re-apply the earlier pull's GROUND+ (no LLM/network)
  env -u PYTHONPATH $PY $SB/topic_pull.py "$B" "$I" --replay 2>&1 | grep '^\[topic-pull' | tee -a "$L"
fi
if [ "$ROOM_TOPIC_PULL" = "1" ] && [ -z "${RESUME_FROM:-}" ]; then   # fresh run: the full pull
  TT0=$(date +%s)
  # S-analysis-memo (6): TOPIC-PULL CACHE — 24 h + sha (the units file + the record layer + the puller + the corpus stamp).
  #   A pull inside the window on unchanged units is REPLAYED from topic_pull.json (no LLM, no network) instead of re-run.
  TPL_STAMP=$I/topic_pull.sha256
  TPL_SHA=$( { cat $B/m2/OUR_ISSUES_${MSLUG}.md $I/record.md $SB/topic_pull.py 2>/dev/null; echo "$GP_CORPUS ${ROOM_TOPIC_PULL_PER_UNIT:-2}"; } | sha256sum | cut -c1-64)
  TPL_AGE=$([ -f $I/topic_pull.json ] && echo $(( $(date +%s) - $(stat -c %Y $I/topic_pull.json) )) || echo 999999)
  if [ -s $I/topic_pull.json ] && [ "$(cat $TPL_STAMP 2>/dev/null)" = "$TPL_SHA" ] && [ "$TPL_AGE" -lt "${ROOM_TOPIC_PULL_TTL_S:-86400}" ] && [ "${ROOM_FORCE_ALL:-0}" != "1" ]; then
    log "topic-pull cache HIT sha=${TPL_SHA:0:12} (age ${TPL_AGE}s < ${ROOM_TOPIC_PULL_TTL_S:-86400}s TTL) — replaying its GROUND+ lines, no pull"
    env -u PYTHONPATH $PY $SB/topic_pull.py "$B" "$I" --replay 2>&1 | grep '^\[topic-pull' | tee -a "$L"
  else
  log "topic-pull cache MISS (stamp=$(cat $TPL_STAMP 2>/dev/null | cut -c1-12 || echo none) want=${TPL_SHA:0:12}, age ${TPL_AGE}s) — pulling"
  env -u PYTHONPATH CUDA_VISIBLE_DEVICES= timeout ${ROOM_TOPIC_PULL_TIMEOUT_S:-1500} $PY $SB/topic_pull.py "$B" "$I" --max-per-unit ${ROOM_TOPIC_PULL_PER_UNIT:-2} 2>&1 | grep -v -i warning | grep -v '^\[retrieval\]' | tee "$I/topic_pull.log" | grep '^\[topic-pull' | tail -3 | cut -c1-300 | tee -a "$L"
  [ -s $I/topic_pull.json ] && echo "$TPL_SHA" > $TPL_STAMP
  # S-self-invalidation (2026-11-16, user: "we have delta"): the pack and prefetch stamps carry GP_CORPUS = size+mtime of
  # chunk_text.db (kept — a pack cached before an EXTERNAL ingest must miss, Cintas/State Equipment 10-28). But topic-pull's
  # OWN ingest just changed that mtime, so the next launch missed both caches and rebuilt 20 min of unchanged work every run
  # (3× tonight). The pull's GROUND+ lines are already applied into the pack above, so the pack on disk IS the post-ingest pack:
  # re-stamp pack + prefetch with the post-pull corpus stat. An ingest by anyone else still invalidates.
  GP_CORPUS_POST=$(stat -c '%s %Y' ~/turbovec/chunk_text.db 2>/dev/null)
  if [ "$GP_CORPUS_POST" != "$GP_CORPUS" ] && [ -s $I/ground_pack.md ]; then
    { cat $I/adverse.md $I/verified_record.md $SBP/forum_rules.json $SBP/ground_pack.py $SBP/rebuttal_table.py $GP_POINTS $ORDER_OCR $PLEADING_TXT 2>/dev/null; echo "$GP_CORPUS_POST ${GP_REBUTTAL:-1}"; } | sha256sum | cut -c1-64 > $GP_STAMP
    { cat $I/record.md $I/adverse.md $I/verified_record.md $SB/room_prefetch.py 2>/dev/null; echo "$GP_CORPUS_POST $ROOM_PREFETCH_PULL"; } | sha256sum | cut -c1-64 > $PF_STAMP
    log "topic-pull ingested into the corpus (stat $GP_CORPUS -> $GP_CORPUS_POST): pack + prefetch re-stamped for the post-ingest corpus (next launch HITs; an external ingest still misses)"
  fi
  log "topic-pull done in $(( $(date +%s) - TT0 ))s; opinions=$($PY -c "import json;print(len(json.load(open('$I/topic_pull.json'))['ingested']))" 2>/dev/null || echo ?) statutes=$($PY -c "import json;print(sum(1 for s in json.load(open('$I/topic_pull.json'))['statutes'] if s.get('status')=='ingested'))" 2>/dev/null || echo ?); stamp ${TPL_SHA:0:12} written"
  fi
fi
# priors: ground seeds from the previous room run of this bundle, if present (no analysis_runner cells to seed from)
PRIORS=""; [ -f $B/room_inputs_vacate2/ground_seeds.md ] && PRIORS=<home>/$B/room_inputs_vacate2/ground_seeds.md
RL=output/research/rooms_ab_${TAG}_relay.log; T2=$(date +%s)
# S-chain-resume (run 54: rooms closed at 3089 s with 272 cards, the 3600 s chain timeout killed the tail one minute later and
# every card was orphaned). RESUME_FROM=cells skips the rooms and runs cards_to_cells -> stub -> m1 -> packet from the
# cards already on disk for $TAG (relay log must carry RELAY EXIT 0).
# ---- the room, AS A FUNCTION: round 1 runs the full roster over every unit; a CLOSURE round re-opens the room on the OPEN
#   units only (ROOM_UNITS=<n,n,…> — dispatch_tmux filters the units layer of every producer prompt and every lane slice).
#   S-tmux-dispatch (DEFAULT since 2026-11-01 — measured 607 s vs 960 s hosted; ROOM_DISPATCH=relay opts back into the hosted
#   relay): every lane runs as its own `hermes -p asp-<lane> chat` process in tmux (scripts/rooms/dispatch_tmux.py, roster
#   $ROOM_ROSTER, step-0 slice gate), writes the same $RL ('ROOM n wall …' + 'RELAY EXIT n') and the same
#   ${TAG}_relay_r<n>_cards.md files. Docs: 00_system/ROOM_DISPATCH_tmux.md
room_launch(){                      # $1 tag  $2 inputs dir (repo-relative)  $3 relay log (repo-relative)  [$4 caption suffix]
  local _t=$1 _i=$2 _rl=$3 _cap="$ROOM_CAPTION${4:-}"
  if [ "${ROOM_DISPATCH:-tmux}" = "tmux" ]; then
    ROOM_ROSTER=${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml}
    log "room dispatch=tmux roster=$ROOM_ROSTER tag=$_t units=${ROOM_UNITS:-ALL} (lanes/waves/timeouts come from the roster, not CHAIN)"
    ROOM_PRIORS=$PRIORS ROOM_CAPTION="$_cap" \
      ROOM_PROP_FORM="$PROP_FORM_BASE" \
      timeout 7200 $PY -u $SB/dispatch_tmux.py $_t <home>/$_i --roster $ROOM_ROSTER --log <home>/$_rl ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
    grep -q "RELAY EXIT" $_rl || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $_rl
  else
  ( cd ~/.hermes/hermes-agent && ROOM_FETCH=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300} ROOM_WAVES=1 ROOM_WAVE_MAX=${ROOM_WAVE_MAX:-4} ROOM_WAVE_ROOM_LANES=2 ROOM_LANE_SLICES=1 ROOM_INPUTS=<home>/$_i ROOM_PRIORS=$PRIORS \
    ROOM_CAPTION="$_cap" \
    ROOM_CHAIN="$CHAIN" \
    ROOM_PROP_FORM="$PROP_FORM_BASE" \
    timeout 7200 venv/bin/python3 -u <home>/$SB/relay_room.py $_t 2>&1 | grep --line-buffered -v onnxruntime > <home>/$_rl; echo "RELAY EXIT ${PIPESTATUS[0]}" >> <home>/$_rl )
  fi
}
# ---- (c) CHAIN↔M1 CARD PATH (BACKLOG 2026-11-13): pipeline.m1 instrument_decision() globs <bundle>/m2/room_r*_cards.md, and the
#   chain never put this run's cards there — any stale copy silently routed m1 to the wrong lane (rc=1 "requires --matrix").
#   fresh = this run's r<n> files land as room_r<n>_cards.md (prior copies kept as .bak.<TS>); append = a closure round's files are
#   numbered AFTER the highest published r<n>, so every round's cards stay under m1's glob instead of overwriting round 1.
publish_cards(){                    # $1 tag  $2 fresh|append
  local _t=$1 _mode=${2:-fresh} f n k=0 base=0 TS=$(date +%Y%m%d_%H%M%S)
  if [ "$_mode" = "append" ]; then
    for f in $B/m2/room_r*_cards.md; do [ -f "$f" ] || continue
      n=$(basename "$f" | sed -E 's/room_r([0-9]+)_cards\.md/\1/'); [ "$n" -gt "$base" ] 2>/dev/null && base=$n; done
  fi
  for f in output/research/rooms_ab/${_t}_relay_r*_cards.md; do
    [ -f "$f" ] || continue
    n=$(basename "$f" | sed -E "s/^${_t}_relay_r([0-9]+)_cards\.md/\1/")
    k=$((k+1)); [ "$_mode" = "append" ] && n=$((base+k))
    [ -f $B/m2/room_r${n}_cards.md ] && cp -p $B/m2/room_r${n}_cards.md $B/m2/room_r${n}_cards.md.bak.$TS
    cp -p "$f" $B/m2/room_r${n}_cards.md
  done
  log "cards published ($_mode): $k file(s) of $_t -> $B/m2/room_r<n>_cards.md (prior copies .bak.$TS) — m1 decides from these"
}
# ---- (a) CLOSURE: a unit is CLOSED when the ledger holds a GROUNDED/RECORD row for it from a lane that did NOT exit non-zero.
#   coverage() writes <bundle>/m2/COVERAGE.md (unit -> closed-by-run / open / lanes EXIT≠0) and echoes the OPEN unit list.
#   S-analysis-memo (2026-11-13): the rule itself now lives in scripts/rooms/analysis_memo.py:coverage() — ONE implementation,
#   shared with the pre-room memo gate (a fork would let the memo call a unit closed that cells_to_stub then refuses). The
#   output contract is unchanged: COVERAGE.md on disk, 'OPEN=<csv>' as the last stdout line.
coverage(){                         # $1 round number  -> stdout: the open unit list (comma, empty when closed)
  env -u PYTHONPATH timeout 900 $PY $SB/analysis_memo.py --coverage "$UNITS_MD" "$CELLS" "$B/m2/COVERAGE.md" "$1" output/research/rooms_ab
}

if [ "${RESUME_FROM:-}" = "cells" ]; then log "RESUME_FROM=cells: rooms skipped; using $(ls output/research/rooms_ab/${TAG}_relay_r*_cards.md | wc -l) card files for $TAG"; else
log "room start (ROOM_WAVES=1 ROOM_WAVE_MAX=${ROOM_WAVE_MAX:-4} ROOM_LANE_SLICES=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300}) chain=$CHAIN"
room_launch "$TAG" "$I" "$RL"
T3=$(date +%s)
log "room wall=$((T3-T2))s | $(grep 'RELAY EXIT' $RL) | $(grep -oE 'ROOM [0-9]+ wall [0-9]+s turns [0-9]+ cards [0-9]+' $RL | tr '\n' ';') | slices posted: $(grep -c '(+slice)' $RL) | FAIL-FAST/SILENT: $(grep -cE 'FAIL-FAST |SILENT ' $RL)"
fi
RX=$(grep -oE "RELAY EXIT [0-9]+" $RL | tail -1 | awk '{print $3}'); if [ "${RX:-1}" != "0" ]; then log "STOP: relay exit ${RX:-none} — cards_to_cells/cells_to_stub/m1 skipped (S-chain-gate)"; exit 1; fi
# ---- 2. cards -> cells (ROOM_FORUM=pa: the gate computes the posture deadline from trigger + rule words) -> stub (+ CASE_FACTS) -> m1
#   The ledger is APPEND+UPSERT (cards_to_cells S-room-closure): round 1 and every closure round write the SAME file.
ROOM_FORUM=pa timeout 600 $PY $SB/cards_to_cells.py $TAG <home>/$I $CELLS 2>&1 | tail -1 | cut -c1-400 | tee -a "$L"
publish_cards "$TAG" fresh
OPEN=$(coverage 1 | tail -1 | sed "s/^OPEN=//"); log "coverage round 1: open=[${OPEN:-none}] -> $B/m2/COVERAGE.md"
# ---- 2z. CLOSURE LOOP (S-room-closure): while a unit has no GROUNDED/RECORD row from a lane that exited 0, re-open the room on
#   THOSE units only (ROOM_UNITS=<open>), up to ROOM_MAX_ROUNDS (default 3). Each round: its own tag + relay log, its cards
#   published for m1, its cells UPSERTED into the one ledger, a fresh COVERAGE.md. OPEN is not terminal and no hand merge happens.
ROUND=1
# S-resume-no-reopen (2026-11-16): RESUME_FROM=cells means "the room has spoken — draft from the cards on disk"; re-running
# the closure loop on resume re-opened a 25-min room for the same OPEN unit it had already failed to close. On resume the
# closure loop is off (OPEN units ship as OPEN with ROOM_ALLOW_OPEN=1, as the ledger line below says).
[ -n "${RESUME_FROM:-}" ] && ROOM_MAX_ROUNDS=1
while [ -n "$OPEN" ] && [ "$ROUND" -lt "${ROOM_MAX_ROUNDS:-3}" ]; do
  ROUND=$((ROUND+1)); TAGC=${TAG}c${ROUND}; RLC=output/research/rooms_ab_${TAGC}_relay.log; TC0=$(date +%s)
  log "closure round $ROUND/${ROOM_MAX_ROUNDS:-3}: units [$OPEN] only (tag $TAGC)"
  PRIORS_SAVE=$PRIORS; PRIORS=$(ls <home>/output/research/rooms_ab/${TAG}*_relay_r*_cards.md 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  ROOM_UNITS="$OPEN" room_launch "$TAGC" "$I" "$RLC" " — CLOSURE ROUND $ROUND: unit(s) $OPEN only"
  PRIORS=$PRIORS_SAVE
  log "closure round $ROUND room wall=$(( $(date +%s) - TC0 ))s | $(grep 'RELAY EXIT' $RLC | tail -1) | FAIL-FAST/SILENT: $(grep -cE 'FAIL-FAST |SILENT ' $RLC)"
  RXC=$(grep -oE "RELAY EXIT [0-9]+" $RLC | tail -1 | awk '{print $3}')
  if [ "${RXC:-1}" != "0" ]; then log "closure round $ROUND: relay exit ${RXC:-none} — no cells added; stopping the loop"; break; fi
  ROOM_FORUM=pa timeout 600 $PY $SB/cards_to_cells.py $TAGC <home>/$I $CELLS 2>&1 | tail -1 | cut -c1-400 | tee -a "$L"
  publish_cards "$TAGC" append
  PREV=$OPEN; OPEN=$(coverage $ROUND | tail -1 | sed "s/^OPEN=//"); log "coverage round $ROUND: open=[${OPEN:-none}] (was [$PREV]) -> $B/m2/COVERAGE.md"
  [ "$OPEN" = "$PREV" ] && { log "closure round $ROUND closed nothing — stopping the loop (the open units need law, not another round)"; break; }
done
[ -n "$OPEN" ] && log "LEDGER NOT CLOSED after $ROUND round(s): unit(s) [$OPEN] — cells_to_stub will refuse (ROOM_ALLOW_OPEN=1 to ship anyway)" || log "LEDGER CLOSED after $ROUND round(s): every unit carries a GROUNDED/RECORD row"
# ---- 1m/seal (S-analysis-memo): the room has run and its cards are in the ledger — stamp closed_sha = the CURRENT inputs-sha on
#   every unit the coverage rule now calls CLOSED. That is what makes the NEXT launch skip them: a unit re-opens only when its
#   sha moves off this seal (new record line, new adverse ¶, new authority) or the user forces it.
if [ "${ROOM_ANALYSIS_MEMO:-1}" = "1" ]; then
  env -u PYTHONPATH timeout 600 $PY $SB/analysis_memo.py --seal --units "$UNITS_MD" --cells "$CELLS" --inputs $I \
    --memo $MEMO --cards-dir output/research/rooms_ab --round "$ROUND" 2>/dev/null | grep -m1 '^analysis seal:' | tee -a "$L" || log "analysis memo: seal failed (memo left as it was)"
fi
fi   # MEMO_SKIP: everything above (recharacterization, pack, prefetch, topic-pull, the room, cards_to_cells, the closure loop) is skipped when the ledger is CLOSED
if [ "${DERIVE_STOP:-0}" = "1" ]; then
  V=$B/m2/VEHICLES_${TAG}.md
  $PY - $CELLS "$V" "$ORDER" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
pv = [r for r in rows if (r.get("bot") or r.get("lane") or "") in ("posture", "posture-vehicle", "vehicle-foreclosure", "appeal-preservation", "remedy", "jurisdiction", "void-order")]
# posture rows: one per vehicle (GROUNDED preferred), ordered by the lane's own rank; the other lanes follow
post = [r for r in pv if r.get("kind") == "posture"]; other = [r for r in pv if r.get("kind") != "posture"]
best = {}
for r in sorted(post, key=lambda r: (0 if r.get("status") == "GROUNDED" else 1, r.get("rank") or 99)):
    best.setdefault((r.get("vehicle") or "")[:60], r)
post = sorted(best.values(), key=lambda r: r.get("rank") or 99)
out = [f"# VEHICLES the room found against {sys.argv[3]} (DERIVE_VEHICLE=1 — nothing was given; the user picks)", "",
       "| # | rank | lane | verdict | vehicle / relief | rule (verbatim) | cite | chunk | trigger | deadline | prerequisites | forecloses / sequences |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for i, r in enumerate(post + other, 1):
    g = lambda k, n=160: str(r.get(k) or "").replace("|", "/").replace("\n", " ")[:n]
    out.append(f"| {i} | {g('rank')} | {g('bot') or g('lane')} | {g('verdict') or g('status')} | {g('vehicle') or g('relief_item') or g('proposition', 120)} | {g('rule_verbatim')} | {g('cite')} | {g('chunk_id') or g('chunk') or g('corpus')} | {g('trigger_event', 60)} {g('trigger_date')} | {g('deadline_date')} {g('deadline_rule', 60)} | {g('prerequisites', 100)} | {g('forecloses', 160)} |")
out += ["", f"rows: {len(post)} vehicles (deduped, lane-ranked) + {len(other)} other-lane cells of {len(rows)} cells. Grounded posture: {sum(1 for r in post if r.get('status')=='GROUNDED')}.",
        "Next: FTYPE=<pick> MSLUG=<slug> STUB=<stub> scripts/chain_order_m2room.sh <tag>"]
open(sys.argv[2], "w").write("\n".join(out) + "\n"); print(f"VEHICLES -> {sys.argv[2]} ({len(post)} vehicles + {len(other)} other)")
EOF
  log "DERIVE_VEHICLE done -> $V (chain stops here by design; user picks FTYPE)"; exit 0
fi
CS_OUT=$B/m2/cells_to_stub_${TAG}.out
# ---- 2i. INSTRUMENT ROUND (S-filing-type-on-demand 2026-11-14): m1 drafts ONLY from a SUSTAINED "INSTRUMENT: <name> | CLASS: …" card
#   the room graded. A ledger closed in DERIVE mode before this date (or any run whose posture lane skipped the clause) has none.
#   Rather than a hand --instrument, re-open ONE leg (posture-vehicle; skeptic+proof grade it) with every card of this run as PRIORS and
#   the filing type of record in the caption — the ROOM decides the instrument, the card lands in the bundle like any other round.
has_instrument(){ env -u PYTHONPATH $PY -c "import glob,sys; sys.path.insert(0,'.'); from pipeline import m1_spec_assembler as S; d=S.instrument_decision(sorted(glob.glob('$B/m2/room_r*_cards.md'))); print(d['file'] if d else '')" 2>/dev/null; }   # ONE implementation: m1's own reader
if [ -n "${FTYPE:-}" ] && [ -z "$(has_instrument)" ] && [ "${ROOM_INSTRUMENT_ROUND:-1}" = "1" ]; then
  TAGI=${TAG}inst$(date +%H%M%S); RLI=output/research/rooms_ab_${TAGI}_relay.log; TI0=$(date +%s)
  PRI=$(ls <home>/output/research/rooms_ab/${TAG}*_relay_r*_cards.md 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  log "instrument round: no SUSTAINED INSTRUMENT card in $B/m2 for FTYPE=$FTYPE — one posture-vehicle leg (tag $TAGI, priors $(echo "$PRI" | tr ',' '\n' | grep -c .))"
  ROOM_PRIORS=$PRI ROOM_CAPTION="$ROOM_CAPTION — INSTRUMENT ROUND: the filing type of record is $FTYPE; post exactly ONE card in the posture form whose INSTRUMENT clause names it" \
    ROOM_PROP_FORM_POSTURE="Vehicle 1 of 1: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Trigger: <event + date from the record>. Deadline rule: <verbatim words>. Prerequisites: <what must exist first>. INSTRUMENT: $FTYPE | CLASS: <pleading|brief|application|affidavit|original_proceeding> | ANATOMY: <sections in order per the forum rule, verbatim where the rule names them> | RULE THAT MAKES IT THIS CLASS: “<verbatim>” <cite> [corpus:<chunk>] — ONE card; the skeptic grades the INSTRUMENT decision." \
    timeout 1500 $PY -u $SB/dispatch_tmux.py $TAGI <home>/$I --roster ${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml} --leg-lanes "posture-vehicle" --log <home>/$RLI ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
  grep -q "RELAY EXIT" $RLI || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $RLI
  RXI=$(grep -oE "RELAY EXIT [0-9]+" $RLI | tail -1 | awk '{print $3}')
  if [ "${RXI:-1}" = "0" ]; then publish_cards "$TAGI" append; else log "instrument round: relay exit ${RXI:-none} — no card published"; fi
  log "instrument round wall=$(( $(date +%s) - TI0 ))s | sustained card: $(has_instrument | xargs -r basename || echo NONE)"
  [ -z "$(has_instrument)" ] && { log "STOP: no SUSTAINED INSTRUMENT card for $FTYPE after the instrument round — m1 will not draft (the decision is graded before any draft)"; exit 1; }
fi

# ---- 2j. GROUND ROUND (user-locked 2026-11-14: the BOTS decide the enumerated ground per unit; m1 only drafts). One posture-vehicle leg over
#   ALL units: for each unit one card 'UNIT <n>: <title> — GROUND: <cite>(<k>) "<verbatim ground>" [corpus] | NONE → <where it belongs>'.
#   The skeptic grades FIT (ground vs the action as pleaded). pipeline.m1_profile_assembler reads the SUSTAINED cards; no LLM planner.
has_grounds(){ env -u PYTHONPATH $PY -c "import glob,sys; sys.path.insert(0,'.'); from pipeline import m1_spec_assembler as S; d=S.ground_decisions(sorted(glob.glob('$B/m2/room_r*_cards.md'))); print(len(d) if d else '')" 2>/dev/null | grep -v onnx | tail -1; }
if [ -n "${FTYPE:-}" ] && [ "${ROOM_GROUND_ROUND:-1}" = "1" ] && [ -z "$(has_grounds)" ]; then
  TAGG=${TAG}grnd$(date +%H%M%S); RLG=output/research/rooms_ab_${TAGG}_relay.log; TG0=$(date +%s)
  PRG=$(ls <home>/output/research/rooms_ab/${TAG}*_relay_r*_cards.md 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  log "ground round: no SUSTAINED GROUND cards in $B/m2 for FTYPE=$FTYPE — one posture-vehicle leg over all units (tag $TAGG)"
  ROOM_PRIORS=$PRG ROOM_LANE_TIMEOUT_S=${ROOM_GROUND_LANE_TIMEOUT_S:-1500} ROOM_CAPTION="$ROOM_CAPTION — GROUND ROUND: the instrument of record is $FTYPE. Post exactly ONE card for EVERY unit open in this room — a unit without a card is a defect. The ONLY grounds are the eight enumerated in Pa.R.C.P. 1028(a)(1)-(8), quoted verbatim from the rule text in the ground pack: (1) jurisdiction/venue/service, (2) conform to law or rule of court, (3) insufficient specificity, (4) legal insufficiency (demurrer), (5) lack of capacity / nonjoinder / misjoinder, (6) pendency of a prior action or agreement for ADR, (7) failure to exercise or exhaust a statutory remedy, (8) full, complete and adequate non-statutory remedy at law. Any other rule (1019, 1024, 1026, 1029, 1028(c)) is AUTHORITY under a ground, never the ground. A ground must FIT the action as pleaded — mortgage foreclosure is an action at law, (a)(8) does not reach it. A constitutional issue (due process / art. I § 11) is raised under (a)(1) expressly, not routed to NONE. NONE only for a unit that is a defense or claim (new matter / counterclaim), an affidavit fact (1028(c)(2)) or a brief point. What Pa.R.C.P. 1147 says the complaint SHALL set forth (itemized amount due, the mortgage, the default) is measured under (a)(2)/(a)(3) — an unitemized or conflicting amount is a PO ground, not a defense; the real-party / holder question is (a)(5) with Pa.R.C.P. 2002; a debt the plaintiff itself reported discharged is (a)(4). SKEPTIC: post one VERDICT per UNIT card, keyed UNIT <n> — a card without your verdict is NOT graded and is not in the objections." \
    ROOM_PROP_FORM_POSTURE="UNIT <n>: <unit title> — GROUND: Pa.R.C.P. 1028(a)(<k>) “<that ground's verbatim words from the rule>” [corpus:<chunk>] | or GROUND: NONE → <new matter|counterclaim|affidavit|brief> — FIT: <why that ground reaches the complaint as pleaded, quoting the complaint's own words> — AUTHORITY: <the rule/statute the unit rests on, verbatim, cite> — RECORD: <facts + dates + doc> — one card per unit; the skeptic grades the FIT and that the quoted ground text is in 1028(a)." \
    timeout 1800 $PY -u $SB/dispatch_tmux.py $TAGG <home>/$I --roster ${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml} --leg-lanes "posture-vehicle" --log <home>/$RLG ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
  grep -q "RELAY EXIT" $RLG || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $RLG
  RXG=$(grep -oE "RELAY EXIT [0-9]+" $RLG | tail -1 | awk '{print $3}')
  if [ "${RXG:-1}" = "0" ]; then publish_cards "$TAGG" append; else log "ground round: relay exit ${RXG:-none} — no cards published"; fi
  log "ground round wall=$(( $(date +%s) - TG0 ))s | SUSTAINED ground cards: $(has_grounds || echo 0)"
  [ -z "$(has_grounds)" ] && { log "STOP: no SUSTAINED GROUND cards after the ground round — m1 will not draft (the room decides grounds)"; exit 1; }
fi
# S-room-closure: cells_to_stub REFUSES an unclosed ledger (exit 3, naming the open units). The chain passes --allow-open only
# when the user said so (ROOM_ALLOW_OPEN=1) — a filing never ships over an unanalyzed unit by accident.
ALLOW_OPEN=""; [ "${ROOM_ALLOW_OPEN:-0}" = "1" ] && ALLOW_OPEN="--allow-open"
run_cells_to_stub(){ timeout 600 $PY -m pipeline.m2.cells_to_stub --cells $CELLS --units $UNITS_MD $ALLOW_OPEN --case $SLUG --court $COURT --type $FTYPE --out $M/$STUB.md --stance $STANCE --counts "$COUNTS" --write-cf > $CS_OUT 2>&1; CS_RC=$?; tail -9 $CS_OUT | tee -a "$L"; [ "$CS_RC" = "3" ] && log "STOP: cells_to_stub exit 3 — UNCLOSED LEDGER (see $B/m2/COVERAGE.md); re-run the room on the open units or ROOM_ALLOW_OPEN=1"; return 0; }
zero_units(){ grep -oE 'units with ZERO grounded pinned rows: \[[^]]*\]' $CS_OUT | tail -1 | grep -oE '\[[^]]*\]' | tr -d '[] '; }
run_cells_to_stub; ZERO=$(zero_units)
# ---- S-pack-coverage (po-16 2026-11-15; deterministic GATE, class-level): every GROUND+ authority in the pack must be carried by
#   ≥ 1 cell (applied or expressly rejected). The skeptic's COVERAGE line was never read by anything; po-16 closed with 14 of 36 pack
#   authorities (holder definition, § 3308(a), Am. Jur., Black's RPI) in no cell — grounded, never argued. Unused → the re-queue leg
#   runs authority-stack + definitions over the units of record with the unused list in the header. PACK_COVERAGE=0 disables.
PC_OUT=$B/m2/PACK_COVERAGE_${TAG}.md; PC_UNUSED=""
if [ "${PACK_COVERAGE:-1}" = "1" ] && [ -s $I/ground_pack.md ]; then
  PC_UNUSED=$($PY $SB/pack_coverage.py --pack $I/ground_pack.md --cells $CELLS --out $PC_OUT 2>/dev/null | grep '^UNUSED:' | sed 's/^UNUSED: //' | paste -sd ';')
  log "pack coverage: $(tail -1 $PC_OUT 2>/dev/null | head -c 160) $(grep -m1 '^pack authorities' $PC_OUT 2>/dev/null)"
  if [ -n "$PC_UNUSED" ]; then
    PC_UNITS=${ROOM_UNITS:-$(grep -oE '^\*\*[0-9]+\.' $B/m2/OUR_ISSUES_${MSLUG}.md | tr -dc '0-9\n' | paste -sd,)}
    ZERO=$(echo "$ZERO,$PC_UNITS" | tr ',' '\n' | grep -E '^[0-9]+$' | sort -un | paste -sd,)
    ROOM_RQ_LANES_ADD="${ROOM_RQ_LANES_ADD:+$ROOM_RQ_LANES_ADD,}authority-stack,definitions"
    PC_HDR=" PACK COVERAGE GAP — these pack authorities (GROUND+) were carried by NO card: $PC_UNUSED. For each: apply it by its DEFINITION to the unit it reaches with the chunk id, or post one card stating why it does not apply. A card carries EVERY authority that grounds it, ';'-separated."
    log "pack coverage GAP: $(echo "$PC_UNUSED" | tr ';' '\n' | grep -c .) unused → re-queue units [$ZERO] + authority-stack,definitions"
  fi
fi
# REQUEUE_UNITS=<n,n> (class knob, any case): also re-queue units that got rows but no LAW (e.g. after a topic-pull landed new
# authority for them) — union with the zero set; the lanes come from the unit heading like any other re-queue.
if [ -n "${REQUEUE_UNITS:-}" ]; then ZERO=$(echo "$ZERO,$REQUEUE_UNITS" | tr ',' '\n' | grep -E '^[0-9]+$' | sort -un | paste -sd,); fi
# ---- 2a. ZERO-row unit re-queue (S-zero-unit-requeue, run 50; T2, chain level, ONE pass): the units cells_to_stub reports with no
#   grounded pinned row go alone to $B/m2/OUR_ISSUES_requeue.md (their blocks verbatim, numbering kept so a card's 'Issue n' is the unit n);
#   the relay runs once more as a single leg of the TWO lanes the first zero unit's heading keyword maps to (a table of names, not
#   reasoning; default verbatim-rule,element-proof), ROOM_PRIORS = this run's cards, TAG=<tag>rq, inputs = ${I}_rq (same pack, copied);
#   cards_to_cells -> cells_room_m2room_rq.jsonl APPENDED to the run's cells; cells_to_stub re-run. Still zero -> 'NOT CARDED: unit n', continue.
if [ -n "$ZERO" ] && [ "${REQUEUE:-1}" = "1" ]; then
  TQ0=$(date +%s); RQ=$B/m2/OUR_ISSUES_requeue.md; IQ=${I}_rq; mkdir -p $IQ/slices
  # unique re-queue tag per run: dispatch_tmux skips lanes whose .done already exists in the ledger, so a re-used ${TAG}rq replayed
  # the PREVIOUS run's cards (09:00 run: 'requeue room wall=0s', new GROUND+ never seen). History kept; cards_to_cells reads by TAGQ.
  TAGQ=${TAG}rq$(date +%H%M%S)
  $PY - $B/m2/OUR_ISSUES_${MSLUG}.md "$ZERO" > $RQ <<'EOF'
import re, sys
md = open(sys.argv[1], errors="ignore").read(); want = {int(x) for x in sys.argv[2].split(",") if x.strip()}
print(md.splitlines()[0] if md else ""); print()
for m in re.finditer(r"^\*\*(\d+)\. (.+?)\*\*\s*$(.*?)(?=^\*\*\d+\. |\Z)", md, re.M | re.S):
    if int(m.group(1)) in want: print(m.group(0).rstrip()); print()
EOF
  # S-rq-lanes-per-unit (run 51): run 50 mapped only the FIRST zero unit's keyword (14 'Rule…' → default verbatim-rule,element-proof) and
  #   the lanes numbered their cards 'Issue 86…106' → 0 rows for units 14/16. Now: every zero unit's heading keyword → its lane pair
  #   (table of names), union of the pairs ≤ 4 lanes (two 2-lane rooms), ONE leg; the rq PROP_FORM pins Issue <n> = the unit number.
  RQ_LANES=""; KW=""
  for u in ${ZERO//,/ }; do
    kw=$(grep -m1 -oE "^\*\*$u\. [A-Za-z0-9-]+" $RQ | sed -E 's/^\*\*[0-9]+\. //' | tr 'A-Z' 'a-z')
    case "$kw" in                                   # unit heading's leading keyword -> two CHAIN lanes
      stare*|hierarchy*)        pair=stare-decisis,authority-stack ;;
      impartial*|code*)         pair=procedural,violation ;;
      forum*)                   pair=posture-vehicle,jurisdiction ;;
      dismissal*)               pair=dismissal-posture,vacatur-sequence ;;
      mandatory*)               pair=verbatim-rule,discretion-closer ;;
      prima*)                   pair=demurrer-test,element-proof ;;
      rule*)                    pair=verbatim-rule,order-silence ;;
      licensee*)                pair=verbatim-rule,element-proof ;;
      the*)                     pair=order-silence,element-proof ;;
      *)                        pair=verbatim-rule,element-proof ;;
    esac
    KW="$KW $u:$kw>$pair"
    for ln in ${pair//,/ }; do case ",$RQ_LANES," in *",$ln,"*) ;; *) RQ_LANES="${RQ_LANES:+$RQ_LANES,}$ln" ;; esac; done
  done
  # ROOM_RQ_LANES_ADD=<lane,lane> (class knob): lanes the user adds to the re-queue leg on top of the keyword pairs — e.g. authority-stack
  # when a run came back with rule text but no treatise shelf (2026-11-14: Am Jur 2d Abatement/Courts/Evidence held + on point, never pulled).
  for ln in ${ROOM_RQ_LANES_ADD//,/ }; do case ",$RQ_LANES," in *",$ln,"*) ;; *) RQ_LANES="${RQ_LANES:+$RQ_LANES,}$ln" ;; esac; done
  RQ_LANES=$(echo "$RQ_LANES" | tr ',' '\n' | head -6 | paste -sd,)
  timeout 600 $PY $SB/room_inputs.py --adverse $B/adverse.md --points $RQ --facts $C/CASE_FACTS.md --sections "$SECTIONS" --counts "$COUNTS" --out $IQ 2>&1 | tail -1 | cut -c1-200 | tee -a "$L"
  cp $I/ground_pack.md $I/ground_table.md $I/face_table.md $IQ/
  # S-rq-slices-fresh (po-16 2026-11-15): the re-queue slices must be built from $IQ's OWN verified_record (the re-queued unit text),
  # never copied from the main room's $I/slices — a unit whose wording changed between legs made the slice index miss its heading,
  # the slice gate STOPped (F3 'index lacks Point [n]') and the leg silently produced nothing while the chain went on to m1.
  rm -f $IQ/slices/*.md; CUDA_VISIBLE_DEVICES= timeout 600 $PY $SB/room_inputs.py --facts $C/CASE_FACTS.md --out $IQ --lane-slices "$RQ_LANES" 2>&1 | grep -v Warning | tail -1 | cut -c1-200 | tee -a "$L"
  for ln in ${RQ_LANES//,/ }; do [ -s $IQ/slices/$ln.md ] || { [ -f $I/slices/$ln.md ] && cp $I/slices/$ln.md $IQ/slices/ && log "requeue slice $ln: fresh build empty — fell back to main-room slice"; }; done
  PRQ=$(ls <home>/output/research/rooms_ab/${TAG}_relay_r*_cards.md 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  RLQ=output/research/rooms_ab_${TAGQ}_relay.log
  log "requeue start units=[$ZERO] kw=$KW lanes=$RQ_LANES priors=$(echo "$PRQ" | tr ',' '\n' | grep -c .) inputs=$IQ tag=$TAGQ"
  if [ "${ROOM_DISPATCH:-tmux}" = "tmux" ]; then   # S-tmux-dispatch: same opt-in as the main room; ONE leg of $RQ_LANES from the roster's defaults
    ROOM_PRIORS=$PRQ ROOM_CAPTION="$ROOM_CAPTION — RE-QUEUE: unit(s) $ZERO only.${PC_HDR:-}" \
      ROOM_PROP_FORM="$PROP_FORM_BASE RE-QUEUE NUMBERING: Issue <n> is the UNIT NUMBER of the re-queued point you address — this leg lists units $ZERO; use exactly those numbers (a card numbered outside $ZERO is discarded), never continue the priors' numbering." \
      timeout 1500 $PY -u $SB/dispatch_tmux.py $TAGQ <home>/$IQ --roster ${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml} --leg-lanes "$RQ_LANES" --log <home>/$RLQ ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
    grep -q "RELAY EXIT" $RLQ || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $RLQ
  else
  ( cd ~/.hermes/hermes-agent && ROOM_FETCH=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300} ROOM_WAVES=1 ROOM_WAVE_MAX=${ROOM_WAVE_MAX:-4} ROOM_WAVE_ROOM_LANES=2 ROOM_LANE_SLICES=1 ROOM_INPUTS=<home>/$IQ ROOM_PRIORS=$PRQ \
    ROOM_CAPTION="$ROOM_CAPTION — RE-QUEUE: unit(s) $ZERO only.${PC_HDR:-}" \
    ROOM_CHAIN="$RQ_LANES" \
    ROOM_PROP_FORM="$PROP_FORM_BASE RE-QUEUE NUMBERING: Issue <n> is the UNIT NUMBER of the re-queued point you address — this leg lists units $ZERO; use exactly those numbers (a card numbered outside $ZERO is discarded), never continue the priors' numbering." \
    timeout 1500 venv/bin/python3 -u <home>/$SB/relay_room.py $TAGQ 2>&1 | grep --line-buffered -v onnxruntime > <home>/$RLQ; echo "RELAY EXIT ${PIPESTATUS[0]}" >> <home>/$RLQ )
  fi
  log "requeue room wall=$(( $(date +%s) - TQ0 ))s | $(grep 'RELAY EXIT' $RLQ) | $(grep -oE 'ROOM [0-9]+ wall [0-9]+s turns [0-9]+ cards [0-9]+' $RLQ | tr '\n' ';') | FAIL-FAST/SILENT: $(grep -cE 'FAIL-FAST |SILENT ' $RLQ)"
  RXQ=$(grep -oE "RELAY EXIT [0-9]+" $RLQ | tail -1 | awk '{print $3}')
  if [ "${RXQ:-1}" = "0" ]; then
    ROOM_FORUM=pa timeout 600 $PY $SB/cards_to_cells.py $TAGQ <home>/$IQ $B/m2/cells_room_m2room_rq.jsonl 2>&1 | tail -1 | cut -c1-400 | tee -a "$L"
    if [ -s $B/m2/cells_room_m2room_rq.jsonl ]; then
      # only the re-queued units' rows travel (run 50: the rq lanes also numbered priors' cards as 'Issue 86…99' → new_units noise)
      grep -E "\"claim\": ?(${ZERO//,/|})[,} ]" $B/m2/cells_room_m2room_rq.jsonl >> $CELLS
      log "requeue: $(grep -cE "\"claim\": ?(${ZERO//,/|})[,} ]" $B/m2/cells_room_m2room_rq.jsonl) of $(grep -c . $B/m2/cells_room_m2room_rq.jsonl) cell row(s) appended to cells_room_m2room.jsonl (claims: $(grep -oE '"claim": ?[0-9]+' $B/m2/cells_room_m2room_rq.jsonl | grep -oE '[0-9]+' | sort -n | uniq -c | tr -s ' \n' ' '))"
    else log "requeue: cards_to_cells emitted no cells"; fi
    run_cells_to_stub; ZERO2=$(zero_units)
  else
    log "requeue relay exit ${RXQ:-none} — no cells added"; ZERO2=$ZERO
  fi
  for u in ${ZERO2//,/ }; do log "NOT CARDED: unit $u (after re-queue $TAGQ lanes=$RQ_LANES) — continuing"; done
  log "requeue done wall=$(( $(date +%s) - TQ0 ))s zero_before=[$ZERO] zero_after=[$ZERO2]"
fi
# ---- 2b. STEP 9 assert (S-operative-ledger): no OPERATIVE FACTS line contradicted by the premium ledger — the stale class is
#   a FAILED draft (date + amount) stated as a payment/draw without its reversal, or a reversal dated other than the ledger's
#   reversal date. EVERY value comes from evidence/_ledger_totals.json (failed[] rows + reversal date); S-no-hardcode 2026-11-14
#   removed the ORDER_17 fallbacks ("2025-11-25"/"81.67"/REV="2025-12-01") — they asserted ONE case's premium ledger against every
#   case's CASE_FACTS. No ledger file / no failed[] rows -> the assert SKIPS and says so (rc 0); it never invents a row.
$PY - "$C/CASE_FACTS.md" "$C/evidence/_ledger_totals.json" <<'EOF' 2>&1 | tee -a "$L"; rc9=${PIPESTATUS[0]}
import json, os, re, sys, datetime as dt
cf, lt = open(sys.argv[1], errors="ignore").read(), sys.argv[2]
sec = re.search(r"^## OPERATIVE FACTS.*?$(.*?)(?=^## |\Z)", cf, re.M | re.S); sec = sec.group(1) if sec else ""
L = json.load(open(lt)) if os.path.exists(lt) else {}
failed = [(f.split()[0], f.split()[1].lstrip("$")) for f in L.get("failed", []) if len(f.split()) >= 2]
if not failed:
    print(f"[operative-ledger] SKIP: no failed[] rows in {lt} — nothing to assert (a ledger row is never assumed)"); sys.exit(0)
# the reversal date is the ledger's, under any of the keys the evidence writer uses; absent -> the
# reversal-DATE clause is not checked (the data does not state it), only the presence of a reversal.
REV = next((str(L[k]) for k in ("reversal_date", "reversed_on", "reversed") if isinstance(L.get(k), str)), None)
if REV is None:
    _rv = L.get("reversals") or L.get("reversed_rows") or {}
    if isinstance(_rv, dict) and _rv:
        REV = str(sorted(_rv.values())[0])
def iso(s):
    for fmt in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%B %d %Y"):
        try: return dt.datetime.strptime(re.sub(r"\s+", " ", s.strip()), fmt).strftime("%Y-%m-%d")
        except ValueError: pass
    return s
def forms(d):
    d = dt.datetime.strptime(d, "%Y-%m-%d"); return {d.strftime("%Y-%m-%d"), d.strftime("%m/%d/%Y"), d.strftime("%B %d, %Y"), d.strftime("%B %-d, %Y"), d.strftime("%b. %-d, %Y")}
bad = []
for l in sec.splitlines():
    for d, amt in failed:
        if amt in l and any(f in l for f in forms(d)) and not ("revers" in l.lower() and (REV is None or any(f in l for f in forms(REV)))):
            bad.append(("failed draft stated without its reversal", l))
    if REV is not None:
        for m in re.finditer(r"revers\w*\s+(?:on\s+|date:?\s*)?((?:[A-Z][a-z]+\.? \d{1,2},? \d{4})|\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", l):
            if iso(m.group(1)) != REV: bad.append((f"reversal dated {m.group(1)} \u2260 {REV}", l))
if bad:
    for why, l in bad: print(f"[operative-ledger] STALE ({why}): {l[:200]}")
    print(f"[operative-ledger] FAIL: {len(bad)} OPERATIVE FACTS line(s) contradicted by the ledger — m1 NOT run"); sys.exit(2)
print(f"[operative-ledger] PASS: {len(sec.splitlines())} OPERATIVE FACTS lines; failed drafts {failed}; reversal {REV if REV else 'NOT STATED IN LEDGER (date clause not checked)'}")
EOF
if [ "${rc9:-1}" != "0" ]; then log "STOP: operative-ledger assert rc=$rc9 — m1 skipped (STEP 9)"; exit 2; fi
fi  # RESUME_FROM=m1
T4=$(date +%s); log "m1 start"
# --cells: the gated cells the chain JUST built. Without it the spec lane drafted from m2/room_r*_cards.md (raw, stale: nothing writes
# them after the first full room) and every re-queue's cells never reached the drafter (4 runs 2026-11-13, ¶124 unchanged).
timeout 1800 $PY -u -m pipeline.m1 $M/$STUB.md --court $COURT --type $FTYPE --case $SLUG --cells $CELLS --out $M/${STUB}_DRAFT.md > $M/m1.log 2>&1; rc=$?
T5=$(date +%s)
# ---- S-draft-review (class, 2026-11-13): the SKEPTIC over the DRAFT. Rooms grade cards; nothing graded the draft, so 'does Compl. ¶96 plead
# what ¶117 says?' reached the user. Deterministic guardrails (missing Compl. ¶, conduct averment answered as law) STOP; skeptic REJECTED
# (cell/cite choice) → ONE redraft loop (M1_REDRAFT = the verdicts), then WARN + review attached to the packet. DRAFT_REVIEW=0 disables.
if [ "${DRAFT_REVIEW:-1}" = "1" ] && [ "$rc" = "0" ]; then
  RVC="$PY $SB/draft_review.py $M/${STUB}_DRAFT.md $PLEADING_TXT $ORDER_OCR $CELLS"
  timeout 400 $RVC 2>&1 | grep -v onnx | tail -1 | cut -c1-300 | tee -a "$L"; rv=${PIPESTATUS[0]}
  if [ "$rv" = "4" ]; then
    log "draft-review: skeptic REJECTED $(cat $M/${STUB}_DRAFT.redraft.json | cut -c1-200) — one redraft loop"
    M1_REDRAFT="$(cat $M/${STUB}_DRAFT.redraft.json)" timeout 1800 $PY -u -m pipeline.m1 $M/$STUB.md --court $COURT --type $FTYPE --case $SLUG --cells $CELLS --out $M/${STUB}_DRAFT.md > $M/m1.redraft.log 2>&1 || log "draft-review: redraft m1 rc=$?"
    timeout 400 $RVC 2>&1 | grep -v onnx | tail -1 | cut -c1-300 | tee -a "$L"; rv=${PIPESTATUS[0]}
    [ "$rv" = "4" ] && log "draft-review: WARN — skeptic still rejects $(cat $M/${STUB}_DRAFT.redraft.json | cut -c1-160); review attached to PACKET (user decides)"
  fi
  if [ "$rv" = "2" ] || [ "$rv" = "3" ]; then log "STOP: draft-review rc=$rv (see $M/${STUB}_DRAFT.review.md) — packet not built"; exit 1; fi
  mkdir -p $M/PACKET && cp $M/${STUB}_DRAFT.review.md $M/PACKET/98_DRAFT_REVIEW.md 2>/dev/null
fi
log "m1 rc=$rc wall=$((T5-T4))s | $(grep -E 'assembled application|cite-gate|\[gate\]' $M/m1.log | cut -c1-240 | tr '\n' ' ')"
ls -la $M | tail -8 | tee -a "$L"
if [ "$rc" = "0" ]; then
  export SLUG B M COURT FTYPE L
  # The packet renders the file m1 just wrote (--out above), never a stale *_M1_DRAFT.docx (r55: the tail picked a 4-hour-old
  # pre-STAND draft because the names differed). Labels follow STANCE: stand = no "Leave to Amend" anywhere in the packet.
  export PACKET_DRAFT_DOCX=$M/${STUB}_DRAFT.docx
  if [ "$STANCE" = "stand" ]; then
    export PACKET_MOTION_NAME="${PACKET_MOTION_NAME:-00_Motion_to_Vacate}" PACKET_INDEX_TITLE="${PACKET_INDEX_TITLE:-# PACKET INDEX — Motion to Vacate (42 Pa.C.S. § 5505)}"
    export PACKET_MOTION_DESC="${PACKET_MOTION_DESC:-Motion to Vacate the Order entered October 22, 2026 (42 Pa.C.S. § 5505) — Relief: vacate; reinstate Counts II, III, V, VI; answer within 20 days; disqualify and reassign (Code of Judicial Conduct Rule 2.11(A)) — incl. RELIEF SOUGHT, VERIFICATION (§ 4904), CERTIFICATE OF SERVICE, [PROPOSED] ORDER}"
    [ -s $M/EXHIBITS_stand.md ] && { export PACKET_MANIFEST=$M/EXHIBITS_stand.md; log "packet manifest = $PACKET_MANIFEST (STANCE=stand: no K/L)"; }
  fi
  source scripts/chain_packet_tail.sh; packet_tail 2>&1 | tail -4 | tee -a "$L"
else log "packet tail skipped (m1 rc=$rc)"; fi
log "E2E total $((T5-T0))s (no analysis_runner step) TAG=$TAG"
