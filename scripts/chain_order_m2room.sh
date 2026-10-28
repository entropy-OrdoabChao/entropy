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
SLUG=${SLUG:-example_v_insurer}; C=obsidian_sync/cases/$SLUG
ORDER=${ORDER:-ORDER_17}; COUNTS=${COUNTS:-II,III,V,VI}; BUNDLE=${BUNDLE:-_bundle13_order17}; B=$C/drafts/$BUNDLE
TAG=${1:-${TAG:-order17m2r49}}; I=$B/room_inputs_m2room; L=$B/m2/chain_m2room.log; PY=/usr/bin/python3
ORDER_OCR=${ORDER_OCR:-$C/intake/_ocr/ccp_order_260100001_notice_of_order_on_case_.pdf.txt}
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
MSLUG=${MSLUG:-vacate}; STUB=${STUB:-motion_to_vacate}; M=$B/motions/${MSLUG}_m2room; mkdir -p $I $M
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
  export ROOM_PROP_FORM_POSTURE="Vehicle <n> of ALL: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Trigger: <event + date from the record>. Deadline rule: <verbatim words>. Prerequisites: <what must exist first>. Wins: <what the court can then do>. Forecloses/sequences: <which other vehicles this one waives, moots, or must precede/follow, with the rule words>. Rank: <1..n> because <reason>."
  ROOM_CAPTION=${ROOM_CAPTION:-"$(${PY:-/usr/bin/python3} -c "import json;print(json.load(open('cases_index/case_registry.json'))['progressive']['caption'])" 2>/dev/null) — $ORDER (Control No. ${CONTROL_NO:-}): what are ALL the doors"}
  MSLUG=derive; STUB=vehicles; DERIVE_STOP=1; GP_TYPE=_all
fi
STANCE_LINE=""; [ "$STANCE" = "stand" ] && STANCE_LINE=" STANCE (the movant's decision, not yours): the complaint STANDS as pleaded; no amendment relief, no Rule 1033 vehicle; ${VEHICLE_LINE:-vehicle = 42 Pa.C.S. § 5505 (modify or rescind within 30 days of entry); relief = vacate $ORDER, reinstate Counts $COUNTS as pleaded, Defendant to answer} in 20 days, and Rule 2.11(A) disqualification/reassignment where the impartiality unit is sustained — Vehicle-form and Relief-form cards state THAT vehicle and THOSE reliefs (a Relief card for leave to amend / an Amended Complaint is discarded)."
# S-card-unit-pin (run 53): cards_to_cells pins a card to its unit by CONTENT against these unit blocks (the lane's 'Issue n' is a hint)
export ROOM_UNITS_MD=<home>/$B/m2/ADVERSE_POINTS.md
# the m2 roster as room lanes (one leg per axis group; skeptic+proof grade every leg, as today). Legs ';', lanes ','.
# leg 1 RULE+FACE: procedural (Axis 4 procedure/posture), verbatim-rule, violation (Axis 4b row), element-proof (Axis 3)
# leg 2 AUTHORITY: authority-stack (binding/hierarchy), authority-holding (Axis 5b what it held), stare-decisis, discretion-closer (RECORD closer)
# leg 3 RELIEF: void-order, remedy, posture-vehicle (vehicle/trigger/rule words — the gate computes the date), jurisdiction
CHAIN=${CHAIN:-"order-silence,demurrer-test,verbatim-rule,procedural,violation,element-proof;jurisdiction,void-order,stare-decisis,authority-stack,authority-holding,appeal-preservation;discretion-closer,dismissal-posture,vacatur-sequence,vehicle-foreclosure,posture-vehicle,remedy"}
LANES=$(echo "$CHAIN" | tr ';' ',')
# ---- ROOM_PROP_FORM (S-mirror-in-prop-form, run 51): the card SHAPE lanes follow is THIS text (relay brief 'PROPOSITION FORM BY LANE'),
#   not the lane's spec skill — run 49/50 agent.log: tool_turns=0 on 33 of 36 lane turns, so asp-<lane>-spec/SKILL.md is never in the
#   prompt; the 3 lanes that happened to skill_view it in run 49 produced 55 of the 57 MIRROR cards. The skeptic reads the same brief
#   and follows its verdict criteria over SOUL (0 NO-MIRROR verdicts in 7 turns with the SOUL rules in its system prompt). Hence MIRROR,
#   the depth floor and the skeptic's NO-MIRROR verdict all live here, once, for the main legs and the re-queue.
PROP_FORM_BASE="ONE violation row in the filing voice: \"Issue <n> — Rule: “<verbatim shall/must words>” <cite> [corpus:<chunk_id>]. Violation: <what $ORDER (Control No. 26010000, entered 2026-10-22) did instead, with the record fact>. MIRROR: <≤160-word paragraph: dollars/acts → parties reversed → structural beneficiary → doctrine-vs-count → Rule 2.11(A) question; no motive labels>\" — the MIRROR clause is appended inside the Violation clause of EVERY Issue-form card (a sibling card on the same Issue may write 'MIRROR: as in #<k>', k = your own card number; Vehicle-form and Relief-form cards carry none). DEPTH: one Issue-form card for every numbered point your axis reaches (your slice lists them) — fewer cards than points reached is an incomplete turn. <axis> is your own lane name (never another lane's). skeptic: an Issue-form card with no MIRROR paragraph and no 'MIRROR: as in #<k>' pointer → VERDICT … | OPEN | NO MIRROR; 'fraud on the court'/'corrupt'/'bias' asserted as fact → REJECTED | BANNED LABEL. No 'pro se'; the movant is sui juris.$STANCE_LINE"
log(){ echo "[$(date +%T)] $*" | tee -a "$L"; }
T0=$(date +%s)
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
  $PY - "$ORDER_OCR" "$C/CASE_FACTS.md" "$B/m2/OUR_ISSUES_${MSLUG}.md" <<'EOF'
import os, re, sys
order = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
facts = open(sys.argv[2], encoding="utf-8", errors="ignore").read() if os.path.exists(sys.argv[2]) else ""
paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", order) if re.search(r"ORDER|DECREE|sustain|overrul|dismiss|grant|den(y|ied)|strike|shall|prejudice", p, re.I)]
out = ["# ISSUES — derived from the order itself (DERIVE_VEHICLE; no vehicle given)"]
for i, p in enumerate(paras[:12], 1):
    out.append(f"\n## Issue {i}\nThe order states: \"{p[:600]}\"\nQuestion: which procedural doors does the written law open against THIS act, in this forum, and on what words?")
el = re.search(r"(?s)##\s*(Element chain|ELEMENT CHAIN|Position of record).*?(?=\n## |\Z)", facts)
if el: out.append("\n## Record facts\n" + " ".join(el.group(0).split())[:2500])
open(sys.argv[3], "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"derive points: {len(paras[:12])} issues from the order")
EOF
else
  sed "s/^PLAINTIFF'S POSITION OF RECORD (CASE_FACTS element chain, verbatim): */Record facts: /; s/^PLAINTIFF'S POSITION OF RECORD[^:]*: */Record facts: /" $B/m2/ADVERSE_POINTS.md > $B/m2/OUR_ISSUES_${MSLUG}.md
fi
RB=$([ -f $I/record.md ] && stat -c %s $I/record.md || echo 0)
timeout 600 $PY $SB/room_inputs.py --adverse $B/adverse.md --points $B/m2/OUR_ISSUES_${MSLUG}.md --facts $C/CASE_FACTS.md \
  --sections "$SECTIONS" --counts "$COUNTS" --out $I 2>&1 | tail -2 | tee -a "$L"
log "record.md bytes before=$RB after=$(stat -c %s $I/record.md) (counts $COUNTS)"
# ---- 1a. ground_pack cache (S-ground-pack-cache, run 50): the pack is a pure function of adverse.md + verified_record.md +
#   forum_rules.json + ground_pack.py (≈ 7 min CPU). sha256 of those four, stored beside the pack in $I/ground_pack.sha256;
#   a matching stamp with the pack on disk → skip (HIT); anything else → rebuild by the SAME command (byte-identical path) + restamp.
GP_STAMP=$I/ground_pack.sha256
GP_SHA=$(cat $I/adverse.md $I/verified_record.md $SBP/forum_rules.json $SBP/ground_pack.py | sha256sum | cut -c1-64)
TG0=$(date +%s)
if [ -s $I/ground_pack.md ] && [ "$(cat $GP_STAMP 2>/dev/null)" = "$GP_SHA" ]; then
  log "ground_pack cache HIT sha=${GP_SHA:0:12} ($(stat -c %s $I/ground_pack.md) B, built $(stat -c %y $I/ground_pack.md | cut -c1-19)) — ground_pack.py skipped"
else
  log "ground_pack cache MISS (stamp=$(cat $GP_STAMP 2>/dev/null | cut -c1-12 || echo none) want=${GP_SHA:0:12}) — rebuilding"
  GROUND_PACK_RULE_CHARS=700 timeout 900 $PY $SBP/ground_pack.py $I/adverse.md $I/verified_record.md $I/ground_pack.md \
    --court $COURT --type ${GP_TYPE:-$FTYPE} --case $CASE_ID 2>&1 | grep -viE "warning|onnx|batches" | tail -3 | tee -a "$L"
  [ -s $I/ground_pack.md ] && echo "$GP_SHA" > $GP_STAMP
  log "ground_pack rebuilt in $(( $(date +%s) - TG0 ))s; stamp ${GP_SHA:0:12} written"
fi
# slices need ground_pack.md + verified_record.md in place -> built after the pack (one ≤ 12 KB file per lane)
CUDA_VISIBLE_DEVICES= timeout 600 $PY $SB/room_inputs.py --facts $C/CASE_FACTS.md --out $I --lane-slices "$LANES" 2>&1 | grep -v Warning | tail -1 | cut -c1-300 | tee -a "$L"
# priors: ground seeds from the previous room run of this bundle, if present (no analysis_runner cells to seed from)
PRIORS=""; [ -f $B/room_inputs_vacate2/ground_seeds.md ] && PRIORS=<home>/$B/room_inputs_vacate2/ground_seeds.md
RL=output/research/rooms_ab_${TAG}_relay.log; T2=$(date +%s)
# S-chain-resume (run 54: rooms closed at 3089 s with 272 cards, the 3600 s chain timeout killed the tail one minute later and
# every card was orphaned). RESUME_FROM=cells skips the rooms and runs cards_to_cells -> stub -> m1 -> packet from the
# cards already on disk for $TAG (relay log must carry RELAY EXIT 0).
if [ "${RESUME_FROM:-}" = "cells" ]; then log "RESUME_FROM=cells: rooms skipped; using $(ls output/research/rooms_ab/${TAG}_relay_r*_cards.md | wc -l) card files for $TAG"; else
log "room start (ROOM_WAVES=1 ROOM_WAVE_MAX=2 ROOM_LANE_SLICES=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300}) chain=$CHAIN"
# S-tmux-dispatch (opt-in, default OFF = the hosted-room relay below, byte-identical): ROOM_DISPATCH=tmux runs every lane as its own
#   `hermes -p asp-<lane> chat` process in tmux (scripts/rooms/dispatch_tmux.py, roster $ROOM_ROSTER, step-0 slice gate), writes the
#   same $RL ('ROOM n wall …' lines + 'RELAY EXIT n') and the same ${TAG}_relay_r<n>_cards.md files. Docs: 00_system/ROOM_DISPATCH_tmux.md
if [ "${ROOM_DISPATCH:-}" = "tmux" ]; then
  ROOM_ROSTER=${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml}
  log "room dispatch=tmux roster=$ROOM_ROSTER (lanes/waves/timeouts come from the roster, not CHAIN)"
  ROOM_PRIORS=$PRIORS ROOM_CAPTION="${ROOM_CAPTION:-Filer v. Example Insurer, Phila. CCP Sept. Term 2026 No. 00586 (Control No. 26010000) — Motion to Vacate $ORDER (42 Pa.C.S. § 5505)}" \
    ROOM_PROP_FORM="$PROP_FORM_BASE" \
    timeout 7200 $PY -u $SB/dispatch_tmux.py $TAG <home>/$I --roster $ROOM_ROSTER --log <home>/$RL ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
  grep -q "RELAY EXIT" $RL || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $RL
else
( cd ~/.hermes/hermes-agent && ROOM_FETCH=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300} ROOM_WAVES=1 ROOM_WAVE_MAX=2 ROOM_WAVE_ROOM_LANES=2 ROOM_LANE_SLICES=1 ROOM_INPUTS=<home>/$I ROOM_PRIORS=$PRIORS \
  ROOM_CAPTION="${ROOM_CAPTION:-Filer v. Example Insurer, Phila. CCP Sept. Term 2026 No. 00586 (Control No. 26010000) — Motion to Vacate $ORDER (42 Pa.C.S. § 5505)}" \
  ROOM_CHAIN="$CHAIN" \
  ROOM_PROP_FORM="$PROP_FORM_BASE" \
  timeout 7200 venv/bin/python3 -u <home>/$SB/relay_room.py $TAG 2>&1 | grep --line-buffered -v onnxruntime > <home>/$RL; echo "RELAY EXIT ${PIPESTATUS[0]}" >> <home>/$RL )
fi
T3=$(date +%s)
log "room wall=$((T3-T2))s | $(grep 'RELAY EXIT' $RL) | $(grep -oE 'ROOM [0-9]+ wall [0-9]+s turns [0-9]+ cards [0-9]+' $RL | tr '\n' ';') | slices posted: $(grep -c '(+slice)' $RL) | FAIL-FAST/SILENT: $(grep -cE 'FAIL-FAST |SILENT ' $RL)"
fi
RX=$(grep -oE "RELAY EXIT [0-9]+" $RL | tail -1 | awk '{print $3}'); if [ "${RX:-1}" != "0" ]; then log "STOP: relay exit ${RX:-none} — cards_to_cells/cells_to_stub/m1 skipped (S-chain-gate)"; exit 1; fi
# ---- 2. cards -> cells (ROOM_FORUM=pa: the gate computes the posture deadline from trigger + rule words) -> stub (+ CASE_FACTS) -> m1
ROOM_FORUM=pa timeout 600 $PY $SB/cards_to_cells.py $TAG <home>/$I $B/m2/cells_room_m2room.jsonl 2>&1 | tail -1 | cut -c1-400 | tee -a "$L"
if [ "${DERIVE_STOP:-0}" = "1" ]; then
  V=$B/m2/VEHICLES_${TAG}.md
  $PY - $B/m2/cells_room_m2room.jsonl "$V" "$ORDER" <<'EOF'
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
run_cells_to_stub(){ timeout 600 $PY -m pipeline.m2.cells_to_stub --cells $B/m2/cells_room_m2room.jsonl --units $B/m2/ADVERSE_POINTS.md --case $SLUG --court $COURT --type $FTYPE --out $M/$STUB.md --stance $STANCE --counts "$COUNTS" --write-cf > $CS_OUT 2>&1; tail -9 $CS_OUT | tee -a "$L"; }
zero_units(){ grep -oE 'units with ZERO grounded pinned rows: \[[^]]*\]' $CS_OUT | tail -1 | grep -oE '\[[^]]*\]' | tr -d '[] '; }
run_cells_to_stub; ZERO=$(zero_units)
# ---- 2a. ZERO-row unit re-queue (S-zero-unit-requeue, run 50; T2, chain level, ONE pass): the units cells_to_stub reports with no
#   grounded pinned row go alone to $B/m2/OUR_ISSUES_requeue.md (their blocks verbatim, numbering kept so a card's 'Issue n' is the unit n);
#   the relay runs once more as a single leg of the TWO lanes the first zero unit's heading keyword maps to (a table of names, not
#   reasoning; default verbatim-rule,element-proof), ROOM_PRIORS = this run's cards, TAG=<tag>rq, inputs = ${I}_rq (same pack, copied);
#   cards_to_cells -> cells_room_m2room_rq.jsonl APPENDED to the run's cells; cells_to_stub re-run. Still zero -> 'NOT CARDED: unit n', continue.
if [ -n "$ZERO" ] && [ "${REQUEUE:-1}" = "1" ]; then
  TQ0=$(date +%s); RQ=$B/m2/OUR_ISSUES_requeue.md; IQ=${I}_rq; TAGQ=${TAG}rq; mkdir -p $IQ/slices
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
  RQ_LANES=$(echo "$RQ_LANES" | tr ',' '\n' | head -6 | paste -sd,)
  timeout 600 $PY $SB/room_inputs.py --adverse $B/adverse.md --points $RQ --facts $C/CASE_FACTS.md --sections "$SECTIONS" --counts "$COUNTS" --out $IQ 2>&1 | tail -1 | cut -c1-200 | tee -a "$L"
  cp $I/ground_pack.md $I/ground_table.md $I/face_table.md $IQ/; for ln in ${RQ_LANES//,/ }; do [ -f $I/slices/$ln.md ] && cp $I/slices/$ln.md $IQ/slices/; done
  PRQ=$(ls <home>/output/research/rooms_ab/${TAG}_relay_r*_cards.md 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  RLQ=output/research/rooms_ab_${TAGQ}_relay.log
  log "requeue start units=[$ZERO] kw=$KW lanes=$RQ_LANES priors=$(echo "$PRQ" | tr ',' '\n' | grep -c .) inputs=$IQ tag=$TAGQ"
  if [ "${ROOM_DISPATCH:-}" = "tmux" ]; then   # S-tmux-dispatch: same opt-in as the main room; ONE leg of $RQ_LANES from the roster's defaults
    ROOM_PRIORS=$PRQ ROOM_CAPTION="Filer v. Example Insurer, Phila. CCP Sept. Term 2026 No. 00586 (Control No. 26010000) — Motion to Vacate $ORDER (42 Pa.C.S. § 5505) — RE-QUEUE: unit(s) $ZERO only" \
      ROOM_PROP_FORM="$PROP_FORM_BASE RE-QUEUE NUMBERING: Issue <n> is the UNIT NUMBER of the re-queued point you address — this leg lists units $ZERO; use exactly those numbers (a card numbered outside $ZERO is discarded), never continue the priors' numbering." \
      timeout 1500 $PY -u $SB/dispatch_tmux.py $TAGQ <home>/$IQ --roster ${ROOM_ROSTER:-obsidian_sync/00_system/rooms/order_m2room.yaml} --leg-lanes "$RQ_LANES" --log <home>/$RLQ ${ROOM_DISPATCH_ARGS:-} 2>&1 | grep --line-buffered -v onnxruntime > /dev/null
    grep -q "RELAY EXIT" $RLQ || echo "RELAY EXIT ${PIPESTATUS[0]}" >> $RLQ
  else
  ( cd ~/.hermes/hermes-agent && ROOM_FETCH=1 ROOM_LANE_TIMEOUT_S=${ROOM_LANE_TIMEOUT_S:-300} ROOM_WAVES=1 ROOM_WAVE_MAX=2 ROOM_WAVE_ROOM_LANES=2 ROOM_LANE_SLICES=1 ROOM_INPUTS=<home>/$IQ ROOM_PRIORS=$PRQ \
    ROOM_CAPTION="Filer v. Example Insurer, Phila. CCP Sept. Term 2026 No. 00586 (Control No. 26010000) — Motion to Vacate $ORDER (42 Pa.C.S. § 5505) — RE-QUEUE: unit(s) $ZERO only" \
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
      grep -E "\"claim\": ?(${ZERO//,/|})[,} ]" $B/m2/cells_room_m2room_rq.jsonl >> $B/m2/cells_room_m2room.jsonl
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
#   "$81.67 … 11/25/2025" stated as a payment/draw (the ledger: 2025-11-25 $81.67 FAILED, reversed 2025-12-01; last retained 2025-10-25)
#   or a reversal date other than 2025-12-01. Reads evidence/_ledger_totals.json when present (failed[] rows), else the ORDER_17 defaults.
$PY - "$C/CASE_FACTS.md" "$C/evidence/_ledger_totals.json" <<'EOF' 2>&1 | tee -a "$L"; rc9=${PIPESTATUS[0]}
import json, os, re, sys, datetime as dt
cf, lt = open(sys.argv[1], errors="ignore").read(), sys.argv[2]
sec = re.search(r"^## OPERATIVE FACTS.*?$(.*?)(?=^## |\Z)", cf, re.M | re.S); sec = sec.group(1) if sec else ""
L = json.load(open(lt)) if os.path.exists(lt) else {}
failed = [(f.split()[0], f.split()[1].lstrip("$")) for f in L.get("failed", []) if len(f.split()) >= 2] or [("2025-11-25", "81.67")]
REV = "2025-12-01"
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
        if amt in l and any(f in l for f in forms(d)) and not ("revers" in l.lower() and any(f in l for f in forms(REV))):
            bad.append(("failed draft stated without its reversal", l))
    for m in re.finditer(r"revers\w*\s+(?:on\s+|date:?\s*)?((?:[A-Z][a-z]+\.? \d{1,2},? \d{4})|\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", l):
        if iso(m.group(1)) != REV: bad.append((f"reversal dated {m.group(1)} ≠ {REV}", l))
if bad:
    for why, l in bad: print(f"[operative-ledger] STALE ({why}): {l[:200]}")
    print(f"[operative-ledger] FAIL: {len(bad)} OPERATIVE FACTS line(s) contradicted by the ledger — m1 NOT run"); sys.exit(2)
print(f"[operative-ledger] PASS: {len(sec.splitlines())} OPERATIVE FACTS lines; failed drafts {failed}; reversal {REV}")
EOF
if [ "${rc9:-1}" != "0" ]; then log "STOP: operative-ledger assert rc=$rc9 — m1 skipped (STEP 9)"; exit 2; fi
T4=$(date +%s); log "m1 start"
timeout 1800 $PY -u -m pipeline.m1 $M/$STUB.md --court $COURT --type $FTYPE --case $SLUG --out $M/${STUB}_DRAFT.md > $M/m1.log 2>&1; rc=$?
T5=$(date +%s)
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
