"""RELAY ROOM — one shared chat per leg, one deterministic ground table IN the chat, a handoff chain of
specialist bots. Nobody re-searches what is on the table; each bot posts its cards and hands off with @next.
Hermes caps a room at 6 members, so the chain runs as consecutive rooms (≤4 lanes + skeptic + proof);
prior rooms' cards are pasted into the next room's opening message.
usage: relay_room.py <tag>   env: ROOM_CHAIN="a,b,c,d;e,f,g;h,i"  ROOM_MAX_S=3000

SPEED (opt-in, default OFF = the original sequential behaviour, byte for byte):
  ROOM_WAVES=1   lanes of a leg run in WAVES of concurrent sub-rooms (the gateway runs at most ONE member turn per
                 room — gateway/hosted_room_discussion.py plan_next_task — so concurrency is only ever ACROSS rooms,
                 up to tui_gateway/hosted_room_driver.py max_concurrent_rooms=4). A lane still sees every card posted
                 before its turn: prior legs' cards + earlier waves' cards are pasted into its sub-room's opening
                 message, and lanes sharing a sub-room see each other in the chat. Lanes in the SAME wave but
                 different sub-rooms do not see each other (that is the trade). Skeptic+proof then run in one final
                 room per leg with every lane card of the leg pasted as THIS leg's (ungraded) cards.
  ROOM_WAVES=2   as 1, plus skeptic and proof run concurrently (two rooms). proof no longer sees skeptic's verdicts.
  ROOM_WAVE_MAX=<n>        concurrent sub-rooms per wave (default 2 — the local-gpu's measured no-preemption point).
  ROOM_WAVE_ROOM_LANES=<k> lanes per sub-room (default 2; a room needs ≥2 members — MIN_DISCUSSION_MEMBERS).
  ROOM_WAVE_SPEC="a,b|c,d" explicit waves (lanes of the leg, '|' between waves); default = inferred from each lane's
                 spec skill text (a lane whose spec names another lane's cards/output runs in a later wave).
  ROOM_WAVE_SINK=<profile> optional never-mentioned 2nd member that allows ROOM_WAVE_ROOM_LANES=1.
  ROOM_OUT=<dir> output dir (default unchanged).
S-m2-room-roster (opt-in, default OFF = byte-identical):
  ROOM_LANE_SLICES=1  SMALL CONTEXT per lane + RELAY-DRIVEN HANDOFF (S-lane-slices-race class fix). The gateway's planner
                 (hosted_room_discussion.plan_next_task) takes the NEWEST user message as the discussion: its @mentions are
                 the round-0 responders, a peer's @mention plans a round-1 turn at once, and a newer user message cancels an
                 in-flight turn (turn.cancelled superseded_by_newer_user_event) whose re-plan then sees ONLY the new message
                 and replies '(pass)' (turn.settled passed=true → room.activity silent_round). So in this mode the slice
                 message IS the trigger of the turn, never a race against a peer's mention:
                   • the opening is de-mentioned except its first member and ends with that member's trigger + slice;
                   • the brief tells members to mention NOBODY (handoff is the relay's job);
                   • when member k-1 is done the relay posts ONE message '@k — your turn now: produce your cards per the
                     RELAY brief above (same form, same ground). Your slice: <ROOM_INPUTS/slices/<k>.md>' (slice part only
                     if the file exists; skeptic/proof get the trigger without a slice);
                   • done = carded (message.member) | SILENT: turn.settled passed=true for k, or no card within
                     ROOM_LANE_TIMEOUT_S (default 420) → ledger FAIL line 'CARD: <k> | SILENT (…) | …' and the relay moves on.
                   • S-ctx-failfast (all three room modes): a turn.failed error or a CARD-less reply carrying 'Context compression' /
                     'context length' / 'maximum context' / an HTTP 4xx shape → 'CARD: <k> | FAILED (…) | …' at once, no redo, no
                     lane-timeout wait, next lane (run 48: authority-holding failed at +1550 s and the room waited 3004 s).
                 Poll 2 s in this mode. Works identically inside a waves sub-room and the grading room(s).
S-relay-stale-room (class fix, always on; a clean run prints nothing new and stays byte-identical):
  a room_id is deterministic (beezle-relay-<tag>-r<gi>[w<wi>-<ri>|g-<grader>]); the gateway hands a LIVE room under that id back
  (create_room idempotent) or raises RoomConflictError, and the old relay REUSED it: after a killed run the room + member sessions
  survive, the new brief lands as a 2nd user message, lanes see a repeat and '(pass)' → silent_round → stall (run 44). Now a live
  room under our id is STALE: it is disbanded (our gateway, its current epoch) and the run's room is created under the nonce-salted
  id <rid>-<ROOM_RUN_NONCE> (a disbanded id is retired for good; default nonce = start time hex); a room the gateway refuses to
  disband (foreign authority / epoch) is left alone and the id is salted the same way. The thread id follows the room id. Before
  leg 1 every live 'RELAY <tag> ' room of this gateway is swept (disbanded). SIGTERM/SIGINT stop every poll loop and, like the
  normal end of the run, the finally disbands every room this run created — a kill never leaves live rooms.
S-wave-room-dup (class fix, always on): the odd lane of a wave is merged into its neighbour room by `last = rooms.pop(); rooms[-1] += last`
  — the old `rooms[-2] += rooms.pop()` stored one index left after the pop (run 46: two rooms ran ['violation','element-proof',
  'verbatim-rule'], ['demurrer-test','procedural'] ran nowhere); run_leg_waves asserts every lane of a wave is in exactly one room.
S-leg-brief-cap (waves only; serial fit() is byte-identical): this leg's lane cards pasted outside the priors (earlier waves' cards
  in a sub-room, the whole leg's in the grading room) are trimmable — fit(build, pl, rec, leg) ladder: record tail (the original first step) → priors → leg
  cards (duplicates, then per-lane cap to the newest n, floor ROOM_LEG_CAP_MIN=4) → FORCED (per-lane cap below the floor, then oldest
  leg lines) → SystemExit only when every block is empty; every step is logged '  [label] fit: <what> −<bytes>B → <size>B'. Identical card lines from two rooms are kept once.
  (run 46: leg 1's 68 cards + the 65,081 B head = 106,445 B > 65,536 → 'opening message cannot fit cap' in the grading room.)
  normal end of the run, the finally disbands every room this run created — a kill never leaves live rooms.
S-gw-wave (opt-in, default OFF = byte-identical):
  ROOM_GATEWAY_WAVE=<n>  hosted-room WAVE turn mode (gateway/hosted_room_discussion.py + tui_gateway/hosted_room_service.py, opt-in
                 per roster: a lane member carries turn_mode='wave', skeptic/proof turn_mode='after', members[0] carries wave_max=<n>;
                 the service reads them from the room row at create_room — nothing module-level). make_room() puts them on the
                 roster; the opening then mentions EVERY lane of the room at once ('@a @b @c — each of you produce your cards now …
                 do not wait for one another; mention nobody') and the gateway runs up to <n> lane turns of that room concurrently
                 (tui_gateway/hosted_room_driver.py: one thread per admitted turn, wave_max slots per room). The relay waits until
                 every lane has carded | turn.settled passed=true | turn.failed | ROOM_LANE_TIMEOUT_S (→ 'CARD: <k> | SILENT (…)'),
                 and ONLY THEN posts skeptic's trigger, waits, posts proof's trigger (relay-driven, one message each — a user
                 message supersedes in-flight turns, so none is posted while a lane still runs). Works per sub-room under ROOM_WAVES
                 too (concurrent local-gpu requests = ROOM_WAVE_MAX × min(ROOM_WAVE_ROOM_LANES, n)); with ROOM_WAVES unset one room
                 per leg → ≤ min(lanes, n). ROOM_LANE_SLICES is not applied to the wave opening (every lane is planned on the one
                 opening; slices would all have to ride in it) — graders' triggers keep the slices shape. A gateway whose
                 validate_roster rejects turn_mode/wave_max (no wave support) → one logged line and the run falls back to the
                 serial roster + serial brief (getattr/try-except, like the list_rooms guard)."""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, os, sys, uuid, time, json, threading, signal
from concurrent.futures import ThreadPoolExecutor
# the relay room runs on the Hermes hosted-room gateway; its install location is config, never a literal home path
sys.path.insert(0, os.environ.get("HERMES_AGENT_HOME", os.path.expanduser("~/.hermes/hermes-agent"))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gateway import hosted_rooms as H
from gateway import hosted_room_discussion as D
import card_gate, relay_priors, room_fetch   # S-fetch-bot: ROOM_FETCH=1 opt-in; default OFF = identity

tag = sys.argv[1] if len(sys.argv) > 1 else "relay1"
ONLY = {int(x) for x in sys.argv[2].split(",")} if len(sys.argv) > 2 else None   # e.g. "2" reruns room 2 alone (priors = earlier rooms' cards of the base tag)
BASE = sys.argv[3] if len(sys.argv) > 3 else tag
# AGNOSTIC: point ROOM_INPUTS at any case's room_inputs/ dir holding adverse.md (the filing we answer),
# record.md (CASE_FACTS/timeline slice), verified_record.md (docket-verified filings/exhibits/control nos.)
# and ground_table.md (built by ground_table.py from adverse.md). ROOM_CAPTION = the case line.
IN = os.environ.get("ROOM_INPUTS", str(_eroot() / "obsidian_sync/cases/example_v_insurer/drafts/_bundle_surreply/room_inputs"))
CAPTION = os.environ.get("ROOM_CAPTION", "Filer v. Progressive, Phila. CCP No. 260100001, Control No. 26010000. Progressive filed a REPLY BRIEF supporting its Preliminary Objections; we answer with a SUR-REPLY.")
OUT = os.environ.get("ROOM_OUT", str(_eroot() / "output/research/rooms_ab"))
def _read(*names):
    for n in names:
        f = f"{IN}/{n}"
        if os.path.exists(f): return open(f, errors="ignore").read()
    return ""
ADVERSE = _read("adverse.md", "reply_brief.md"); RECORD = _read("record.md", "record_slice.md"); VERIFIED = _read("verified_record.md", "po_verified_record.md")
FACE = _read("face_table.md")   # built by face_table.py — deterministic face-of-filing facts
PACK = _read("ground_pack.md")  # built by ground_pack.py — authorities + FORUM RULES + requirements + face + m0 extras (supersedes table+face)
GROUPS = [g.split(",") for g in os.environ.get(
    "ROOM_CHAIN", "procedural,definitions,verbatim-rule,construction;authority-stack,stare-decisis,jurisdiction,duty;remedy,posture-vehicle,pseudolaw-rebut").split(";")]
MAX_S = int(os.environ.get("ROOM_MAX_S", "3000"))
# the court-facing sentence form; default = the rebuttal voice. A concise filing (violation list) sets ROOM_PROP_FORM, e.g.
# "Issue <n> — Rule: “<verbatim shall/must words>” <cite>. Violation: <what the record shows was done instead, ECF n, date>."
PROP_FORM = os.environ.get("ROOM_PROP_FORM", "1–2 sentences in the FILING'S voice — the rule applied to this record, OUR relief (the objection must be overruled / the reply's point fails because …)")
# S-room-axis-forms: each bot specialises on its axis, so the posture and remedy axes have their OWN card form (and their own bin in
# cards_to_cells). LANE_FORM maps lane name → form name; it is a table of names, not reasoning. ROOM_PROP_FORM stays the violation form.
PROP_FORM_POSTURE = os.environ.get("ROOM_PROP_FORM_POSTURE",
    "Vehicle: <name> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Trigger: <event + date from the record>. Deadline rule: <verbatim words>. Standard: <verbatim>. Must contain: <items>.")
PROP_FORM_REMEDY = os.environ.get("ROOM_PROP_FORM_REMEDY",
    "Relief <n>: <what the court is asked to do> — Rule: “<verbatim>” <cite> [corpus:<chunk>]. Because: <record fact>.")
LANE_FORM = {"posture-vehicle": "posture", "dismissal-posture": "posture", "jurisdiction": "posture", "remedy": "remedy", "vacatur-sequence": "remedy", "vehicle-foreclosure": "posture"}   # default: violation
FORMS = {"violation": PROP_FORM, "posture": PROP_FORM_POSTURE, "remedy": PROP_FORM_REMEDY}


def prop_form_block(chain):
    """the PROPOSITION form(s) for this room's lanes: the violation form for every lane, plus the posture/remedy forms for the lanes
    the table maps to them (a posture lane uses the Vehicle form for its vehicle/deadline/standard card and the violation form for a
    violation it finds; jurisdiction uses the Vehicle form only when it speaks to appealability / the vehicle)."""
    by_form = {}
    for lane in chain: by_form.setdefault(LANE_FORM.get(lane, "violation"), []).append(lane)
    lines = [f"PROPOSITION FORM BY LANE — violation lanes ({', '.join(by_form.get('violation', []) or ['all others'])}): <{PROP_FORM}>"]
    if "posture" in by_form:
        lines.append(f"posture lanes ({', '.join(by_form['posture'])}) — your vehicle / deadline / standard / must-contain card uses THIS form, never 'Violation: None': <{PROP_FORM_POSTURE}>; a violation you find still uses the violation form.")
    if "remedy" in by_form:
        lines.append(f"remedy lane ({', '.join(by_form['remedy'])}) — one card per relief item, THIS form: <{PROP_FORM_REMEDY}>")
    return "\n".join(lines)
GW_ID = "install:4c9466e0f6a34fb390d66e521f7a4f29"
ground = open(f"{IN}/ground_table.md").read()
db = H.default_db_path(); gw = H.local_authority_gateway_id()
local = tuple(sorted(p for p in os.listdir(str(_ehermes() / "profiles")) if not p.startswith(".")))

# ---- speed levers (all default OFF) ---------------------------------------------------------------------------
WAVES = int(os.environ.get("ROOM_WAVES", "0"))
WAVE_MAX = max(1, int(os.environ.get("ROOM_WAVE_MAX", "2")))
WAVE_ROOM_LANES = max(1, int(os.environ.get("ROOM_WAVE_ROOM_LANES", "2")))
WAVE_SPEC = os.environ.get("ROOM_WAVE_SPEC", "")
WAVE_SINK = os.environ.get("ROOM_WAVE_SINK", "")
PROFILES = os.environ.get("ROOM_PROFILES_DIR", str(_ehermes() / "profiles"))
LANE_SLICES = int(os.environ.get("ROOM_LANE_SLICES", "0"))
LANE_TIMEOUT_S = int(os.environ.get("ROOM_LANE_TIMEOUT_S", "420"))      # slices mode: a member with no card by then is SILENT
GW_WAVE = int(os.environ.get("ROOM_GATEWAY_WAVE", "0"))                  # S-gw-wave: >0 hosted-room wave turn mode (≤ n lane turns at once), <=0 unchanged
GW_WAVE_OK = GW_WAVE > 0           # cleared (once, logged) when the gateway's validate_roster rejects the wave fields → serial fallback
GRADERS = ("skeptic", "proof")
WAVE_SENTENCE = "This room runs as a WAVE: {ats} — each of you produce your cards now per the RELAY brief below; do not wait for one another; mention nobody by handle (the relay posts skeptic's and proof's turns after every lane has carded)."
SLICE_HDR = "Your slice:"
TRIGGER_HDR = "your turn now:"
_AT = re.compile(r"@(?=[A-Za-z][\w-]*)")                                  # a real @mention (gateway _MENTION_RE shape) → '＠'


def slice_body(lane):
    """ROOM_INPUTS/slices/<lane>.md, other members' handles de-mentioned, or '' (no file / slices off)."""
    f = f"{IN}/slices/{lane}.md"
    if not LANE_SLICES or not os.path.exists(f): return ""
    return _AT.sub("＠", open(f, errors="ignore").read().strip())


def lane_trigger_text(h):
    """ROOM_LANE_SLICES=1: the ONE user message that plans @h's turn. It is the newest user message when @h is planned, so
    the planner's discussion already holds the slice (never a peer's @mention racing a later slice message)."""
    task = ("take your turn exactly as the RELAY brief above instructs for you (same form, same ground)" if h in ("skeptic", "proof")
            else "produce your cards per the RELAY brief above (same form, same ground)")
    body = slice_body(h)
    return f"@{h} — {TRIGGER_HDR} {task}. Mention nobody by handle — the relay posts the next turn." + (f" {SLICE_HDR}\n{body}" if body else "")


def slices_opening(text, first, handles):
    """slices mode: the opening mentions ONLY `first` (every other member @handle → '＠' so no peer is a round-0 responder), the
    handoff lines tell members to mention nobody, and `first`'s trigger (+ slice) closes the message."""
    others = sorted({*handles, "skeptic", "proof"} - {first}, key=len, reverse=True)
    text = re.sub(r"@(" + "|".join(map(re.escape, others)) + r")(?![\w-])", r"＠\1", text)
    text = text.replace("End your message with exactly one line: @<next bot in the order>. The last lane ends with: ＠skeptic",
                        "HANDOFF IS THE RELAY'S JOB: mention NOBODY by handle (a peer you name is given a turn without its slice and is cancelled); end your message after your last card.")
    text = text.replace("≤ 25. End with: ＠proof", "≤ 25. End with: DONE")
    return text.replace(f"@{first} — RELAY —", f"＠{first} — RELAY —", 1) + "\n\n" + lane_trigger_text(first)


def wave_opening(text, chain, handles):
    """S-gw-wave: the ONE opening plans every lane of the room at once — it mentions exactly the room's lanes (every other member
    @handle → '＠'), replaces the serial HANDOFF sentence with the WAVE sentence and forbids member mentions (the relay posts the graders'
    triggers after the wave). A graders-only room (ROOM_WAVES grading room) has no wave: its opening is the slices-shaped trigger of
    its first member (the second grader is relay-triggered)."""
    lanes = [h for h in chain if h not in GRADERS]
    if not lanes: return slices_opening(text, chain[0], handles)
    ats = " ".join(f"@{h}" for h in lanes)
    others = sorted({*handles, *chain, *GRADERS} - set(lanes), key=len, reverse=True)
    text = re.sub(r"@(" + "|".join(map(re.escape, others)) + r")(?![\w-])", r"＠\1", text)
    text = re.sub(r"This room is a HANDOFF: one bot at a time, each works its own axis on the SHARED GROUND and passes to the next\. Order: [^\n]*?\.\n",
                  WAVE_SENTENCE.format(ats=ats) + "\n", text, count=1)
    text = text.replace("End your message with exactly one line: @<next bot in the order>. The last lane ends with: ＠skeptic",
                        "HANDOFF IS THE RELAY'S JOB: mention NOBODY by handle (a peer you name is given a turn without its slice and is cancelled); end your message after your last card.")
    text = text.replace("≤ 25. End with: ＠proof", "≤ 25. End with: DONE")
    return text.replace(f"@{chain[0]} — RELAY —", f"{ats} — RELAY —", 1)
LEG_CARDS_HDR = "==== CARDS POSTED BY THIS LEG'S LANES (parallel sub-rooms — UNGRADED: skeptic grades these as THIS room's cards; proof checks their record facts) ===="
WAVE_CARDS_HDR = "==== CARDS POSTED BY THIS LEG'S EARLIER WAVES (settled — build on them, never redo) ===="
_dblock = threading.Lock()   # H.* sqlite calls from wave threads are serialised; the waiting is not
# ---- S-relay-stale-room ----------------------------------------------------------------------------------------
RUN_NONCE = os.environ.get("ROOM_RUN_NONCE") or f"{int(time.time()):x}"
CREATED = {}        # room_id -> authority_epoch of every live room THIS run created (removed when disbanded)
STOP = False        # set by the SIGTERM/SIGINT handler: every poll loop returns at once, the finally disbands CREATED


def _live_state(rid):
    """room_state of a LIVE room, or None (never created / disbanded / retired id)."""
    try:
        with _dblock: return H.room_state(db, room_id=rid)
    except Exception: return None


def _n_events(rid):
    try:
        with _dblock: return len(H.read_events(db, room_id=rid, since_seq=0)["events"])
    except Exception: return -1


def disband_stale(rid, st, why):
    """disband a live room that is NOT this run's (left by a killed run) — only when it is this gateway's, at its current epoch
    (disband_room fences on both). Returns True when the gateway disbanded it, False when it is foreign / refused."""
    owner = st.get("authority_gateway_id", gw)
    if owner != gw:
        print(f"  stale room {rid} held by {owner} epoch {st.get('authority_epoch')} — not ours, left alone ({why})", flush=True); return False
    n = _n_events(rid)
    try:
        with _dblock: H.disband_room(db, room_id=rid, expected_gateway_id=gw, expected_epoch=int(st.get("authority_epoch") or 1))
    except Exception as e:
        print(f"  stale room {rid} disband refused ({e}) — left alone ({why})", flush=True); return False
    print(f"  stale room {rid} disbanded ({n} events) → recreated ({why})", flush=True); return True


def sweep_stale():
    """startup: disband every live 'RELAY <tag> ' room of this gateway (a killed run's leftovers). Prints one line per room; a clean
    gateway prints nothing. A gateway stub without list_rooms is skipped."""
    lister = getattr(H, "list_rooms", None)
    if lister is None: return
    try:
        with _dblock: rooms = lister(db)
    except Exception as e:
        print(f"  stale sweep skipped ({e})", flush=True); return
    for r in rooms:
        if str(r.get("name") or "").startswith(f"RELAY {tag} ") and r.get("authority_gateway_id") == gw and r.get("room_id") not in CREATED:
            disband_stale(r["room_id"], r, "startup sweep")


def _on_signal(signum, frame):
    global STOP
    STOP = True
    print(f"  signal {signum}: stopping — {len(CREATED)} live room(s) of this run to disband", flush=True)
    raise SystemExit(128 + signum)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try: signal.signal(_sig, _on_signal)
    except (ValueError, OSError): pass          # not the main thread / unsupported platform: no handler, nothing else changes


def post(rid, text, thread):
    with _dblock:
        st = H.room_state(db, room_id=rid)
        H.append_event(db, room_id=rid, event_id=H.user_event_id(f"client-{uuid.uuid4().hex[:12]}"), kind="message.user",
                       actor={"kind": "user", "id": "desktop"}, payload=D.validate_user_payload({"text": text, "thread_id": thread}),
                       authority_gateway_id=gw, authority_epoch=st["authority_epoch"])


def brief(chain, handles, prior_txt, leg_cards_txt="", waves_skeptic=False, form_chain=None):
    """form_chain: the lanes whose PROPOSITION forms are listed (default = chain). In waves mode every sub-room and the grading
    room(s) of a leg pass the WHOLE leg chain, so the shared head is identical across the leg's rooms and the skeptic grades
    against the same forms the lanes were given."""
    order = " → ".join(f"@{h}" for h in handles)
    form_block = prop_form_block(form_chain if form_chain is not None else chain)
    PACK_OR_TABLES = ("==== 3. GROUND PACK (deterministic: adverse authorities → chunks; FORUM RULES verbatim for this court + filing type; filing requirements; FACE table; m0 grounding) ====\n" + PACK) if PACK else (
        "==== 3. GROUND TABLE (deterministic; built once from the adverse filing; chunk_ids are real and already verified) ====\n" + ground + "\n\n==== 4. FACE TABLE (deterministic face-of-filing facts; file:line evidence) ====\n" + FACE)
    # S-lean-opening (user-locked 2026-10-25: "slice it ALL"): with ROOM_LANE_SLICES=1 the per-lane slice already carries each
    # lane's share of the record (units) and the pack (rule blocks); the opening no longer repeats the whole RECORD + VERIFIED +
    # PACK to every lane on every turn (~40 KB / ~10k tokens of prefill per turn). The opening keeps the pack CORE (forum rules +
    # filing requirements — forum-wide, every lane needs them) and points at the slice. ROOM_OPENING_LEAN=0 restores the old opening.
    if LANE_SLICES and os.environ.get("ROOM_OPENING_LEAN", "1") == "1":
        core = PACK
        i = core.find("## FORUM RULES"); j = core.find("# FACE TABLE")
        if i >= 0: core = core[i:(j if j > i else len(core))]
        core = fit_core(core)
        GROUND_SECTIONS = ("==== 2. RECORD + 3. GROUND PACK — delivered PER LANE: your trigger message ends with 'Your slice:' = the record facts, "
            "the units and the pack rule blocks for YOUR axis. Quote only from your slice, a chunk you retrieve this turn, or the FORUM RULES below. ====\n"
            "==== 3a. GROUND PACK CORE (forum rules + filing requirements, verbatim — shared by every lane) ====\n" + core)
    else:
        GROUND_SECTIONS = ("==== 2. RECORD — docket-verified filings, exhibits, control numbers ====\n" + VERIFIED +
            "\n\n==== 2b. RECORD — case facts / timeline slice ====\n" + RECORD + "\n\n" + PACK_OR_TABLES)
    skeptic_extra = (" The cards under 'CARDS POSTED BY THIS LEG'S LANES' below ARE this room's cards — grade every one of them." if waves_skeptic else "")
    text = f"""@{chain[0]} — RELAY — {CAPTION} This room is a HANDOFF: one bot at a time, each works its own axis on the SHARED GROUND and passes to the next. Order: {order}.

SHARED GROUND — everything you need is IN THIS MESSAGE, below: (1) the ADVERSE FILING we answer, (2) the RECORD (what was actually filed, when, with which exhibits and control numbers — docket-verified), (3) the GROUND TABLE: every authority the adverse filing cites, already resolved to its verbatim corpus chunk with a real chunk_id, (3b) the FORUM RULES for this court and filing type, VERBATIM — when a card says a rule is violated or satisfied, quote the rule's words from here; a rule not in the pack is NOT IN CORPUS, never cite it from memory, (4) the FACE TABLE: deterministic facts about the filing's face (caption, exhibits named vs attached, control no., day gaps, signature, how it labels us, oath, their own characterisations). The FACE rows are FACTS with file:line; your job on them is MEANING under the forum's rules — procedural: one CARD per FACE row marked DEFECT/VERIFY stating the rule it offends or satisfies (cite the rule from the table or NOT IN CORPUS); proof: CONFIRM/DENY each FACE row against the record; pseudolaw-rebut/duty: the "their own words" rows are prima facie material — use them. DO NOT read files and DO NOT search for anything on the table — it is all here. Cite record facts as record:<heading or line> and authorities by chunk_id.
Prior bots' cards are above you in this chat (and pasted below if from an earlier room) — build on them, never redo them. If your axis needs an authority NOT on the table, you may run at most 2 searches (tool_call name mcp__beezle__search_chunks) and you MUST post what you found as a new ground line: GROUND+: <cite> | <chunk_id> | "<verbatim excerpt ≤ 40 words>" so nobody after you searches for it again.

YOUR TURN: ≤ 2 tool calls total (searches only, only if the table lacks what your axis needs). definitions / verbatim-rule / construction: ZERO tool calls — the pack's definitions, rule text and chunks are your only sources; a term or rule absent from the pack is NOT IN CORPUS, say so and move on. Budget: your whole turn ≤ 90 seconds of work. Cards only, ≤ 15 lines, no narration. End your message with exactly one line: @<next bot in the order>. The last lane ends with: @skeptic
OUTPUT FORM — one card per line:
CARD: <axis> | <finding> | <record file:line or Ex.> | <authority: cite — "words copied from the table/chunk" — chunk_id, or NOT IN CORPUS> | SUSTAINED|OPEN | PROPOSITION: <your lane's form below. No meta: never mention the sur-reply, the draft, the room, files, cards, or what "should be titled". This sentence goes into the brief verbatim.>
{form_block}
NEW POINTS: a finding that fits none of the numbered points is posted as "Issue <next number>" — it is kept as a NEW unit, never dropped.
CARD: <axis> | CONTRADICTION: <what the movant said then vs now> | <both locations> | — | OPEN | PROPOSITION: <the sentence the brief says about it>
QUOTE DISCIPLINE: inside quotation marks only words copied from the ground table excerpt, a chunk you retrieved this turn, or an input file; chunk_id/file:line right after the quote; paraphrase outside quotes; cannot quote → NOT QUOTED + OPEN. A deterministic gate forces unverifiable quotes OPEN after the room.
STANDING RULES: corpus and enacted text decide; tag [doc]/[corpus:<chunk_id>]/[official:<cite>]; never invent cites, dates, amounts, control numbers; the signer is sui juris — never "pro se".
skeptic: one VERDICT line per card above: VERDICT: <axis> | <card #> | SUSTAINED|REJECTED|OPEN | <reason ≤ 15 words> — grade reasoning against the table AND the PROPOSITION: REJECTED when the proposition claims more than the authority or record supports, argues the movant's side, states a fact not in the record, or contains meta (sur-reply/draft/file names). The table's chunks are already verified, do not re-fetch them. Grade ONLY the cards posted in THIS room (prior-room cards are already graded — never re-grade them).{skeptic_extra} FIRST line(s) of your turn, before any VERDICT: one per GROUND in the pack: COVERAGE: G<n> | <# SUSTAINED cards answering it> | COVERED|GAP — a GAP names what is missing. Then the VERDICT lines, ≤ 25. End with: @proof
proof: you are the filing verifier. One RECORD line per record fact the cards rely on, checked against the RECORD sections below: RECORD: <fact> | CONFIRMED|DENIED|NOT IN RECORD | <record heading/line>. Also one CARD per CONTRADICTION between the adverse filing and the record (dates, exhibits named but absent, control numbers, parties, amounts). End with: DONE.
{prior_txt}{leg_cards_txt}
==== 1. ADVERSE FILING (verbatim) ====
{ADVERSE}

{GROUND_SECTIONS}
"""
    if GW_WAVE_OK: return wave_opening(text, chain, handles)
    return slices_opening(text, chain[0], list(handles) + list(chain)) if LANE_SLICES else text


def card_lines(evs):
    return [l.strip() for e in evs if e["kind"] == "message.member"
            for l in (e["payload"].get("text") or "").splitlines() if l.lstrip().startswith(("CARD:", "VERDICT:", "RECORD:", "GROUND+:"))]


def wave_roster(base):
    """S-gw-wave: the roster the promoted gateway's validate_roster accepts (hosted_room_discussion._validate_member optional fields
    turn_mode / wave_max): every lane member turn_mode='wave', skeptic/proof turn_mode='after' (planned once every wave member of the
    round has a terminal event), members[0] carries wave_max=<n> (room cap = min of the declared member wave_max — _room_wave_max)."""
    members = [{**m, "turn_mode": "after" if m["handle"] in GRADERS else "wave"} for m in base]
    members[0]["wave_max"] = max(1, min(GW_WAVE, int(getattr(D, "MAX_DISCUSSION_MEMBERS", 6))))
    return members


def make_room(rid, name, handles, thread=None):
    """create the room; returns the (room_id, thread_id) the run must use. S-relay-stale-room: a LIVE room already under `rid`
    (create_room idempotent reuse, or RoomConflictError) is stale — disbanded when ours — and the run's room is created under the
    nonce-salted id (a disbanded id is retired for good); the thread id is salted the same way.
    S-gw-wave (ROOM_GATEWAY_WAVE>0): the roster carries the wave fields; a gateway whose validate_roster rejects or drops them has no
    wave support → ONE logged line, GW_WAVE_OK cleared, this and every later room serial (roster + brief), like the list_rooms guard."""
    global GW_WAVE_OK
    members = [{"member_id": h, "profile": h if h in GRADERS else (h if h == WAVE_SINK else f"asp-{h}"), "handle": h,
                "display_name": h.replace("-", " ").title()} for h in handles]
    roster = None
    if GW_WAVE_OK:
        wm = wave_roster(members)
        try:
            roster = D.validate_roster(wm, local_profiles=local)
            if [getattr(m, "turn_mode", "") for m in roster] != [raw["turn_mode"] for raw in wm]: raise ValueError("validate_roster dropped turn_mode")
            members = wm
        except Exception as e:
            print(f"  gateway wave mode unavailable ({type(e).__name__}: {str(e)[:100]}) — ROOM_GATEWAY_WAVE={GW_WAVE} ignored: serial roster + serial handoff", flush=True)
            GW_WAVE_OK = False; roster = None
    if roster is None: roster = D.validate_roster(members, local_profiles=local)
    mem = [{"member_id": m.member_id, "profile": m.profile, "handle": m.handle, "target": dict(m.target or {}), "display_name": m.display_name,
            **({"turn_mode": m.turn_mode} if getattr(m, "turn_mode", "") else {}), **({"wave_max": raw["wave_max"]} if "wave_max" in raw else {})}
           for raw, m in zip(members, roster)]
    def create(r, n):
        with _dblock: room = H.create_room(db, room_id=r, name=n, members=mem, authority_gateway_id=gw)
        room = room if isinstance(room, dict) else {}
        if room.get("idempotent"): return None
        CREATED[r] = int(room.get("authority_epoch") or 1); return r
    try:
        if create(rid, name): return rid, thread
        why = "live room handed back idempotently"
    except H.RoomConflictError as e:
        why = f"RoomConflictError: {e}"
    st = _live_state(rid)
    salted, sthread = f"{rid}-{RUN_NONCE}", (f"{thread}-{RUN_NONCE}" if thread else thread)
    if st is None: print(f"  stale room id {rid} is retired/disbanded ({why}) → creating {salted}", flush=True)
    elif not disband_stale(rid, st, why): print(f"  stale room {rid} stays live → creating {salted}", flush=True)
    else: print(f"  stale room {rid} → {salted}", flush=True)
    if not create(salted, name): raise SystemExit(f"salted room {salted} is itself a live room — set ROOM_RUN_NONCE to a fresh value")
    return salted, sthread


def _lane_of(l):
    """grouping key of a pasted card line: the lane of a CARD, else its prefix (GROUND+ / RECORD / VERDICT)."""
    return l.split(" | ")[0][6:].strip() if l.startswith("CARD:") else l.split(":")[0]


def _line_key(l):
    return re.sub(r"\W+", "", l.split(" | ")[1] if " | " in l else l).lower()[:70]      # relay_priors' finding key


LEG_CAP_MIN = int(os.environ.get("ROOM_LEG_CAP_MIN", "4"))     # waves: per-lane floor of this leg's cards kept for skeptic/proof before the record tail goes


def trim_leg(leg, pl, floor):
    """ONE step of the leg-cards ladder (S-leg-brief-cap). Returns (new_leg, what) or (leg, None) when only the floor is left.
    1. duplicate lines: an exact copy of a prior/earlier leg line (the dup-room bug pasted 2 copies), or a GROUND+/RECORD/VERDICT line
       whose finding key is already present — CARD lines are never key-deduped (two lanes may legitimately share a finding);
    2. per-lane cap to the NEWEST n (n = current max − 1, never below `floor`): every lane keeps its latest cards, so skeptic/proof
       still grade every lane of the leg."""
    seen = set(pl); keys = {_line_key(l) for l in pl if not l.startswith("CARD:")}; out = []
    for l in leg:
        if l in seen: continue
        if not l.startswith("CARD:"):
            k = _line_key(l)
            if k in keys: continue
            keys.add(k)
        seen.add(l); out.append(l)
    if len(out) < len(leg): return out, f"{len(leg) - len(out)} duplicate card line(s)"
    by = {}
    for i, l in enumerate(leg): by.setdefault(_lane_of(l), []).append(i)
    n = max((len(v) for v in by.values()), default=0)
    if n <= floor: return leg, None
    n -= 1; keep = {i for v in by.values() for i in v[-n:]}
    return [l for i, l in enumerate(leg) if i in keep], f"per-lane cap → newest {n} ({len(leg) - len(keep)} line(s))"


def fit_core(core, reserve=None):
    """The ground-pack CORE is the one block the fit() ladder never trims (pack = sacred), so a big forum cell makes the opening
    unfittable before any lane runs (r2a/r2b 2026-10-27: 62-rule _all cell + DEEPEN = 63 KB core, 72 KB opening, 64 KB cap, RELAY
    EXIT 1, 0 slices). Class fix: shorten the FORUM RULE verbatim quotes (`> ...` lines under `### R.`) in steps — 700 → 500 → 350 →
    250 chars — until the core fits the cap minus a reserve for the opening prose + record floor. DEEPEN rows and FILING REQUIREMENTS
    are never cut (they are the cross-cutting depth; already short). Below 250 the rule becomes an INDEX line (cite — why — source/chunk)
    and the lane quotes it from its slice or by retrieving the source. Logged like every other ladder step."""
    CAP = D.MAX_USER_TEXT_BYTES
    reserve = int(os.environ.get("ROOM_CORE_RESERVE", str(12 * 1024))) if reserve is None else reserve
    budget = CAP - reserve
    if len(core.encode()) <= budget: return core
    i = core.find("## DEEPEN"); forum, rest = (core[:i], core[i:]) if i >= 0 else (core, "")
    blocks = re.split(r"(?=^### R\. )", forum, flags=re.M)
    def squeeze(n):
        out = []
        for b in blocks:
            if not b.startswith("### R. "): out.append(b); continue
            if n == 0:
                head = b.split("\n", 1)[0]; src = re.search(r"^source: (\S+)", b, re.M); cid = re.search(r"^chunk: (\S+)", b, re.M)
                out.append(head + f"  [source: {src.group(1) if src else '?'}; chunk: {cid.group(1) if cid else 'NONE'}; verbatim in your slice or retrieve]\n\n")
            else:
                out.append(re.sub(r"(?m)^> (.*)$", lambda m: "> " + (m.group(1)[:n] + ("…" if len(m.group(1)) > n else "")), b))
        return "".join(out)
    for n in (500, 350, 250, 0):
        cand = squeeze(n) + rest
        if len(cand.encode()) <= budget:
            print(f"  fit_core: forum rule quotes → {n or 'INDEX'} chars: {len(core.encode())}B → {len(cand.encode())}B (budget {budget})", flush=True)
            return cand
    print(f"  fit_core: even INDEX form is {len(cand.encode())}B > budget {budget}; passing as-is", flush=True)
    return cand


def fit(build, pl, rec, leg=None, label=""):
    """trim ladder — timeline slice (down to 1.5 KB) → oldest prior CARD lines → GROUND+ tail; never the adverse filing / verified record /
    pack. Default (leg None — the serial room; build(pl, rec)) that is byte for byte the original, silent, SystemExit when the priors are gone.
    S-leg-brief-cap (waves: `leg` = this leg's lane cards pasted OUTSIDE the priors — earlier waves' cards for a sub-room, the whole leg's
    for the grading room(s); build(pl, rec, leg)): a leg must never die because the previous wave/leg was productive, so the leg block is
    trimmable too, AFTER the original ladder (where the live relay raised): trim_leg — duplicates → per-lane cap to the newest n down to
    the LEG_CAP_MIN floor → FORCED (per-lane cap below the floor, then the oldest lines) → SystemExit only when every block is empty. The
    record tail stays the FIRST step on purpose: the production record.md is 59 KB and the serial path already sacrifices it (run 46's
    65,081 B head was post-trim); putting it last would crush every lane's cards to the floor to keep a timeline slice. In waves mode every
    step is logged with what went and by how many bytes (the ledger keeps the full card list)."""
    CAP = D.MAX_USER_TEXT_BYTES; waves = leg is not None
    text = build(pl, rec, leg) if waves else build(pl, rec)
    while len(text.encode()) >= CAP:
        over = len(text.encode()) - CAP + 512; before = len(text.encode())
        if len(rec.encode()) > 2000:
            rec = rec.encode()[:max(1500, len(rec.encode()) - over)].decode(errors="ignore") + "\n[... timeline slice trimmed to fit the room message cap]"
            what = "record: timeline slice tail"
        elif any(l.startswith("CARD:") for l in pl):        # oldest room cards go first …
            cut = max(1, sum(l.startswith("CARD:") for l in pl) // 8); idx = [i for i, l in enumerate(pl) if l.startswith("CARD:")][:cut]
            pl = [l for i, l in enumerate(pl) if i not in set(idx)]; what = f"priors: oldest {cut} CARD line(s)"
        elif pl:
            cut = len(pl)//8 + 1; pl = pl[:len(pl) - cut]; what = f"priors: {cut} GROUND+ tail line(s)"           # … GROUND+ seeds last, least-used (tail) first
        elif not waves: raise SystemExit(f"opening message cannot fit cap even with no priors: {len(text.encode())}")
        elif leg and (step := trim_leg(leg, pl, LEG_CAP_MIN))[1]:
            leg, what = step[0], "leg cards: " + step[1]
        elif leg and (step := trim_leg(leg, pl, 1))[1]:
            leg, what = step[0], f"leg cards FORCED below the {LEG_CAP_MIN}/lane floor: " + step[1]
        elif leg:
            cut = max(1, len(leg) // 8); leg = leg[cut:]; what = f"leg cards FORCED: oldest {cut} line(s)"
        else: raise SystemExit(f"opening message cannot fit cap even with no priors, no leg cards and a 1.5 KB record: {len(text.encode())}")
        text = build(pl, rec, leg) if waves else build(pl, rec)
        if waves: print(f"  {label}fit: {what} −{before - len(text.encode())}B → {len(text.encode())}B (cap {CAP})", flush=True)
    assert len(text.encode()) < CAP, len(text.encode())
    return text, pl


def run_room(rid, thread, handles, text, t0, stop_on, label=""):
    """post the opening message and follow the room until `stop_on` (a member handle) has posted or MAX_S; returns (events, timeline).
    ROOM_LANE_SLICES=1 → run_room_slices (relay-driven handoff; the opening already carries handles[0]'s trigger + slice).
    ROOM_GATEWAY_WAVE>0 (gateway accepted the wave roster) → run_room_gw_wave (every lane on the opening, relay-driven graders)."""
    if GW_WAVE_OK: return run_room_gw_wave(rid, thread, handles, text, t0, stop_on, label)
    if LANE_SLICES: return run_room_slices(rid, thread, handles, text, t0, stop_on, label)
    post(rid, text, thread)
    print(f"{label}{rid} chain {handles} bytes {len(text.encode())} t0 {time.strftime('%H:%M:%S')}", flush=True)
    seen = 0; nudged = set(); timeline = []; synth = []; ctx_failed = set()
    def failed(m, why):      # S-ctx-failfast: FAIL card + hand off to the next lane at once; no redo, no wait
        synth.append({"kind": "message.member", "actor": {"kind": "relay", "id": "relay"}, "payload": {"text": fail_line(m, why), "member_id": "relay"}})
        ctx_failed.add(m); nudged.add(m); nudged.add((m, 2))
        print(f"  {label}+{round(time.time()-t0)}s FAIL-FAST {m}: {why} — FAIL card, no redo, moving on", flush=True)
        if m in handles and m != stop_on:
            nxt = handles[min(handles.index(m) + 1, len(handles) - 1)]
            post(rid, f"@{nxt} — {m} FAILED (context overflow: {why[:40]}); it will not redo. Continue the handoff from the cards above.", thread)
    while time.time() - t0 < MAX_S and not STOP:
        with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
        for e in evs[seen:]:
            k = e["kind"]; p = e.get("payload", {})
            if k == "message.member":
                txt = p.get("text") or ""; timeline.append((round(time.time() - t0), e["actor"]["id"], len(txt)))
                print(f"  {label}+{round(time.time()-t0)}s {e['actor']['id']} chars={len(txt)} cards={sum(l.startswith(('CARD:','VERDICT:','RECORD:','GROUND+:')) for l in txt.splitlines())}", flush=True)
                a = e["actor"]["id"]; cf = stage_ctx_fail([e], a) if a in handles and a not in ctx_failed else ""
                if cf: failed(a, cf); continue
                room_fetch.nudge(e, handles, nudged, lambda m: post(rid, m, thread))      # S-fetch-bot: NOT IN CORPUS -> @fetch (ROOM_FETCH=1 only)
            if k == "turn.failed":
                m = p.get("member_id"); print(f"  {label}+{round(time.time()-t0)}s FAILED {m}: {(p.get('error') or '')[:80]}", flush=True)
                cf = ctx_fail(p.get("error") or "")
                if cf and m in handles and m not in ctx_failed: failed(m, cf); continue
                if m in handles and m not in nudged:
                    nudged.add(m)
                    post(rid, f"@{m} — your turn failed ({(p.get('error') or 'cut')[:60]}). Redo it SHORT: one line per card, ≤ 20 lines, no prose, no repeated lines; then hand off.", thread)
                elif m in handles and m in nudged and (m, 2) not in nudged:
                    nudged.add((m, 2)); nxt = handles[min(handles.index(m) + 1, len(handles) - 1)]
                    post(rid, f"@{nxt} — {m} failed twice; continue the handoff from the cards above.", thread)
        seen = len(evs)
        if any(e["kind"] == "message.member" and e["actor"]["id"] == stop_on for e in evs) or stop_on in ctx_failed: break
        time.sleep(10)
    with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
    return list(evs) + synth, timeline


def silent_line(h, why):
    """the ledger's FAIL card for a member that produced no card (S-lane-slices-race: never wait forever, never drop silently)."""
    return f"CARD: {h} | SILENT ({why}) | — | — | OPEN | PROPOSITION: none — {h} posted no card this room"


# S-ctx-failfast (run 48: authority-holding '+1550s FAILED … Context compression is temporarily paused' then the relay waited the
# full lane window): a turn error / lane reply carrying a context-overflow signature can never recover by a redo — mark the lane
# FAILED at once (FAIL card) and move on. Matches 'Context compression', 'context length', 'maximum context', HTTP 4xx shapes.
CTX_FAIL_RE = re.compile(r"context compression|context[ _-]length|maximum context|context window|prompt is too long|too many tokens"
                         r"|\bHTTP[ /]?4\d\d\b|\bstatus(?: code)?[ :=]+4\d\d\b|\bError code:? 4\d\d\b"
                         r"|\b4\d\d (?:Bad Request|Payload Too Large|Request Entity Too Large|Unprocessable|Too Many Requests|Client Error)\b", re.I)


def ctx_fail(txt):
    """'' or the matched context-overflow signature in a turn.failed error / a lane's reply text."""
    m = CTX_FAIL_RE.search(txt or "")
    return m.group(0) if m else ""


def fail_line(h, why):
    """the ledger's FAIL card for a lane killed by a context-overflow error (S-ctx-failfast: no redo, no lane-timeout wait)."""
    return f"CARD: {h} | FAILED ({why}) | — | — | OPEN | PROPOSITION: none — {h} failed fast: context overflow, lane not retried"


def stage_ctx_fail(stage, cur):
    """S-ctx-failfast: the context-overflow signature in `cur`'s turn.failed error, or in a CARD-less reply of `cur`, within the
    `stage` events; '' when none (a carded reply that merely mentions 'context window' in prose is NOT a failure)."""
    for e in stage:
        p = e.get("payload", {}) or {}
        if e["kind"] == "turn.failed" and p.get("member_id") == cur:
            s = ctx_fail(p.get("error") or "")
            if s: return s
        elif e["kind"] == "message.member" and (e.get("actor") or {}).get("id") == cur:
            txt = p.get("text") or ""
            if not any(l.startswith(("CARD:", "VERDICT:", "RECORD:", "GROUND+:")) for l in txt.splitlines()):
                s = ctx_fail(txt)
                if s: return s
    return ""


def run_room_slices(rid, thread, handles, text, t0, stop_on, label=""):
    """ROOM_LANE_SLICES=1: the relay drives EVERY handoff. handles[0] is planned on the opening (which ends with its trigger +
    slice); member k is started by ONE user message lane_trigger_text(k) posted when member k-1 is DONE. Done = carded
    (message.member) | SILENT: turn.settled passed=true for k (the planner's '(pass)' — visible in the room events) or no card
    within LANE_TIMEOUT_S → silent_line in the returned events (a synthetic relay message, so card_lines() ledgers it) and the
    relay moves on. Stops after `stop_on` is done or MAX_S. Returns (events, timeline) like run_room."""
    post(rid, text, thread)
    print(f"{label}{rid} chain {handles} bytes {len(text.encode())} t0 {time.strftime('%H:%M:%S')} slices: relay-driven handoff, lane timeout {LANE_TIMEOUT_S}s", flush=True)
    seen = 0; nudged = set(); timeline = []; synth = []
    k = 0; stage_idx = 0; t_stage = time.time()
    def start(i, prefix=""):
        nonlocal stage_idx, t_stage
        with _dblock: stage_idx = len(H.read_events(db, room_id=rid, since_seq=0)["events"])
        msg = prefix + lane_trigger_text(handles[i]); post(rid, msg, thread); t_stage = time.time()      # ONE message: a 2nd would cancel the turn
        print(f"  {label}+{round(time.time()-t0)}s trigger → @{handles[i]} bytes {len(msg.encode())}{' (+slice)' if SLICE_HDR in msg else ''}", flush=True)
    def silent(i, why):
        synth.append({"kind": "message.member", "actor": {"kind": "relay", "id": "relay"}, "payload": {"text": silent_line(handles[i], why), "member_id": "relay"}})
        print(f"  {label}+{round(time.time()-t0)}s SILENT {handles[i]}: {why}", flush=True)
    def failed(i, why):      # S-ctx-failfast
        synth.append({"kind": "message.member", "actor": {"kind": "relay", "id": "relay"}, "payload": {"text": fail_line(handles[i], why), "member_id": "relay"}})
        print(f"  {label}+{round(time.time()-t0)}s FAIL-FAST {handles[i]}: {why} — FAIL card, no redo, moving on", flush=True)
    while time.time() - t0 < MAX_S and k < len(handles) and not STOP:
        with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
        for e in evs[seen:]:
            kind = e["kind"]; p = e.get("payload", {})
            if kind == "message.member":
                txt = p.get("text") or ""; timeline.append((round(time.time() - t0), e["actor"]["id"], len(txt)))
                print(f"  {label}+{round(time.time()-t0)}s {e['actor']['id']} chars={len(txt)} cards={sum(l.startswith(('CARD:','VERDICT:','RECORD:','GROUND+:')) for l in txt.splitlines())}", flush=True)
                room_fetch.nudge(e, handles, nudged, lambda m: post(rid, m, thread))      # S-fetch-bot: NOT IN CORPUS -> @fetch (ROOM_FETCH=1 only)
            if kind == "turn.failed":
                m = p.get("member_id"); print(f"  {label}+{round(time.time()-t0)}s FAILED {m}: {(p.get('error') or '')[:80]}", flush=True)
        seen = len(evs)
        cur = handles[k]; stage = evs[stage_idx:]
        cf = stage_ctx_fail(stage, cur)
        if cf: done = "ctx:" + cf
        elif any(e["kind"] == "message.member" and e["actor"]["id"] == cur for e in stage): done = "carded"
        elif any(e["kind"] == "turn.settled" and e["payload"].get("member_id") == cur and e["payload"].get("passed") is True for e in stage): done = "passed"
        elif any(e["kind"] == "turn.failed" and e["payload"].get("member_id") == cur for e in stage) and cur not in nudged:
            nudged.add(cur); done = ""          # one redo: the same trigger (+ slice) again, SHORT — one message, never two
            start(k, prefix=f"@{cur} — your previous turn failed. Redo it SHORT: one line per card, ≤ 20 lines, no prose, no repeated lines.\n")
        elif sum(e["kind"] == "turn.failed" and e["payload"].get("member_id") == cur for e in stage) >= 1 and cur in nudged: done = "failed twice"
        elif time.time() - t_stage > LANE_TIMEOUT_S: done = f"no card within {LANE_TIMEOUT_S}s"
        else: done = ""
        if done:
            if done.startswith("ctx:"): failed(k, done[4:])
            elif done != "carded": silent(k, done if done != "passed" else "turn passed — no card")
            if cur == stop_on: break
            k += 1
            if k < len(handles): start(k)
        time.sleep(2)
    with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
    return list(evs) + synth, timeline


def run_room_gw_wave(rid, thread, handles, text, t0, stop_on, label=""):
    """ROOM_GATEWAY_WAVE>0: stage 0 = every lane of the room, all planned on the ONE opening (wave_opening) and run by the gateway up
    to wave_max at a time; a lane is done when carded (message.member) | turn.settled passed=true | turn.failed (no redo: a redo message
    would supersede the other in-flight lanes) → silent_line; lanes still silent LANE_TIMEOUT_S after the opening → silent_line. Only
    when stage 0 is complete does the relay post ONE trigger per remaining member (skeptic, then proof — lane_trigger_text, like slices
    mode; one redo for a failed grader, it runs alone). A graders-only room has handles[0] on the opening (slices shape) and the rest
    relay-triggered. Stops after `stop_on` is done or MAX_S. Returns (events, timeline) like run_room."""
    lanes = [h for h in handles if h not in GRADERS] or list(handles[:1])
    pending = [h for h in handles if h not in lanes]
    post(rid, text, thread)
    print(f"{label}{rid} chain {handles} bytes {len(text.encode())} t0 {time.strftime('%H:%M:%S')} gw-wave: {len(lanes)} lane(s) on the opening, wave_max {max(1, min(GW_WAVE, int(getattr(D, 'MAX_DISCUSSION_MEMBERS', 6))))}, relay-driven graders, lane timeout {LANE_TIMEOUT_S}s", flush=True)
    seen = 0; nudged = set(); timeline = []; synth = []; done = set()
    stage = list(lanes); stage_idx = 0; t_stage = time.time()
    def start(h, prefix=""):
        nonlocal stage_idx, t_stage, stage
        with _dblock: stage_idx = len(H.read_events(db, room_id=rid, since_seq=0)["events"])
        msg = prefix + lane_trigger_text(h); post(rid, msg, thread); t_stage = time.time(); stage = [h]      # ONE message: a 2nd would cancel the turn
        print(f"  {label}+{round(time.time()-t0)}s trigger → @{h} bytes {len(msg.encode())}{' (+slice)' if SLICE_HDR in msg else ''}", flush=True)
    def silent(h, why):
        synth.append({"kind": "message.member", "actor": {"kind": "relay", "id": "relay"}, "payload": {"text": silent_line(h, why), "member_id": "relay"}})
        print(f"  {label}+{round(time.time()-t0)}s SILENT {h}: {why}", flush=True)
    def failed(h, why):      # S-ctx-failfast
        synth.append({"kind": "message.member", "actor": {"kind": "relay", "id": "relay"}, "payload": {"text": fail_line(h, why), "member_id": "relay"}})
        print(f"  {label}+{round(time.time()-t0)}s FAIL-FAST {h}: {why} — FAIL card, no redo, moving on", flush=True)
    while time.time() - t0 < MAX_S and (stage or pending) and not STOP:
        with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
        for e in evs[seen:]:
            kind = e["kind"]; p = e.get("payload", {})
            if kind == "message.member":
                txt = p.get("text") or ""; timeline.append((round(time.time() - t0), e["actor"]["id"], len(txt)))
                print(f"  {label}+{round(time.time()-t0)}s {e['actor']['id']} chars={len(txt)} cards={sum(l.startswith(('CARD:','VERDICT:','RECORD:','GROUND+:')) for l in txt.splitlines())}", flush=True)
                room_fetch.nudge(e, handles, nudged, lambda m: post(rid, m, thread))      # S-fetch-bot: NOT IN CORPUS -> @fetch (ROOM_FETCH=1 only)
            if kind == "turn.failed":
                m = p.get("member_id"); print(f"  {label}+{round(time.time()-t0)}s FAILED {m}: {(p.get('error') or '')[:80]}", flush=True)
        seen = len(evs); st = evs[stage_idx:]
        for cur in list(stage):
            cf = stage_ctx_fail(st, cur)
            if cf: fin = "ctx:" + cf
            elif any(e["kind"] == "message.member" and e["actor"]["id"] == cur for e in st): fin = "carded"
            elif any(e["kind"] == "turn.settled" and e["payload"].get("member_id") == cur and e["payload"].get("passed") is True for e in st): fin = "turn passed — no card"
            elif any(e["kind"] == "turn.failed" and e["payload"].get("member_id") == cur for e in st):
                if cur not in lanes and cur not in nudged:                     # a relay-triggered grader runs alone: one SHORT redo, one message
                    nudged.add(cur); start(cur, prefix=f"@{cur} — your previous turn failed. Redo it SHORT: one line per card, ≤ 20 lines, no prose, no repeated lines.\n"); break
                fin = "turn failed" + (" twice" if cur in nudged else "")
            elif time.time() - t_stage > LANE_TIMEOUT_S: fin = f"no card within {LANE_TIMEOUT_S}s"
            else: fin = ""
            if fin:
                if fin.startswith("ctx:"): failed(cur, fin[4:])
                elif fin != "carded": silent(cur, fin)
                stage.remove(cur); done.add(cur)
        if not stage:
            if stop_on in done: break
            if pending: start(pending.pop(0))
        time.sleep(2)
    with _dblock: evs = H.read_events(db, room_id=rid, since_seq=0)["events"]
    return list(evs) + synth, timeline


def disband(rid):
    epoch = CREATED.pop(rid, 1)
    try:
        with _dblock: H.disband_room(db, room_id=rid, expected_gateway_id=gw, expected_epoch=epoch)
    except Exception as e: print("  disband:", e)


_DEP_RE = r"\b{o}\b(?:'s|’s)?\s+(?:cards?|lane|bot|output|findings?|turn|room)"


def lane_deps(lane, others):
    """other chain lanes whose OUTPUT the lane's spec skill names (asp-<lane>/skills/legal/<lane>-spec/SKILL.md; SOUL.md is the generic default)."""
    deps = set()
    for f in (f"{PROFILES}/asp-{lane}/skills/legal/{lane}-spec/SKILL.md", f"{PROFILES}/asp-{lane}/SOUL.md"):
        if not os.path.exists(f): continue
        txt = open(f, errors="ignore").read()
        for o in others:
            if o != lane and re.search(_DEP_RE.format(o=re.escape(o)), txt, re.I): deps.add(o)
    return deps


def waves_of(chain):
    """ROOM_WAVE_SPEC wins; else layer the chain by spec-declared dependencies (chain order kept inside a wave)."""
    if WAVE_SPEC:
        w = [[l for l in part.split(",") if l] for part in WAVE_SPEC.split("|")]
        assert sorted(sum(w, [])) == sorted(chain), f"ROOM_WAVE_SPEC lanes {sum(w, [])} != chain {chain}"
        return w
    deps = {l: lane_deps(l, chain) for l in chain}; placed = set(); waves = []
    while len(placed) < len(chain):
        wave = [l for l in chain if l not in placed and deps[l] <= placed]
        if not wave: wave = [next(l for l in chain if l not in placed)]        # cycle → fall back to chain order, one per wave
        waves.append(wave); placed |= set(wave)
    if not WAVE_SINK and WAVE_ROOM_LANES > 1:
        # a room needs 2 members: fold singleton waves — forward into the next wave (deps kept: it still runs after
        # everything it depends on), or, for a singleton LAST wave, back into the previous wave where run_leg_waves
        # merges the single-lane room into its neighbour (the lane runs last there; cards of that room stay visible).
        folded = []
        for i, w in enumerate(waves):
            if len(w) == 1 and len(chain) > 1:
                if i + 1 < len(waves): waves[i + 1] = w + waves[i + 1]
                elif folded: folded[-1] = folded[-1] + w
                else: folded.append(w)
                continue
            folded.append(w)
        waves = folded
    return waves


def run_leg_waves(gi, chain, pl, lines_out, timeline, t0):
    """lanes in waves of concurrent sub-rooms, then skeptic(+proof); appends card lines in wave/chain order."""
    def paste(lines, hdr):
        return ("\n" + hdr + "\n" + "\n".join(lines)) if lines else ""
    prior_hdr = "==== PRIOR CARDS from earlier rooms of this relay (settled — build on them, never redo) ===="
    leg_lines = []     # this leg's lane cards, visible to later waves + skeptic/proof
    rec = RECORD
    for wi, wave in enumerate(waves_of(chain), 1):
        rooms = [wave[i:i + WAVE_ROOM_LANES] for i in range(0, len(wave), WAVE_ROOM_LANES)]
        if WAVE_ROOM_LANES == 1 and not WAVE_SINK: raise SystemExit("ROOM_WAVE_ROOM_LANES=1 needs ROOM_WAVE_SINK (a room needs 2 members)")
        if len(rooms) > 1 and len(rooms[-1]) == 1 and not WAVE_SINK:        # no single-lane room without a sink: merge it into its neighbour
            # S-wave-room-dup: NOT `rooms[-2] += rooms.pop()` — Python loads rooms[-2] BEFORE the pop and stores to rooms[-2] AFTER it,
            # i.e. one index to the left: a 7-lane wave lost ['demurrer-test','procedural'] and ran ['violation',…] in two rooms (run 46).
            last = rooms.pop(); rooms[-1] = rooms[-1] + last
        if len(rooms) == 1 and len(rooms[0]) == 1 and not WAVE_SINK: raise SystemExit(f"wave {wi} of leg {gi} has one lane {rooms[0]} and no ROOM_WAVE_SINK — a room needs 2 members")
        assert sorted(sum(rooms, [])) == sorted(wave) and len(sum(rooms, [])) == len(set(sum(rooms, []))), f"wave {wi} rooms {rooms} != wave {wave}"
        def one(ri_sub):
            ri, sub = ri_sub
            handles = sub + ([WAVE_SINK] if len(sub) == 1 else [])
            rid = f"beezle-relay-{tag}-r{gi}w{wi}-{ri}"; thread = f"relay-{tag}-r{gi}w{wi}-{ri}"
            rid, thread = make_room(rid, f"RELAY {tag} r{gi} w{wi}.{ri}", handles, thread)
            snapshot = list(leg_lines)
            def build(pl_, rec_, leg_):
                return brief(sub, sub, paste(pl_, prior_hdr) + paste(leg_, WAVE_CARDS_HDR), form_chain=chain).replace(RECORD, rec_)
            text, _ = fit(build, pl, rec, snapshot, label=f"[w{wi}.{ri}] ")      # S-leg-brief-cap: earlier waves' cards are trimmable too
            evs, tl = run_room(rid, thread, handles, text, t0, stop_on=sub[-1], label=f"[w{wi}.{ri}] ")
            disband(rid)
            return ri, card_lines(evs), tl
        with ThreadPoolExecutor(max_workers=WAVE_MAX) as ex:
            results = sorted(ex.map(one, enumerate(rooms)), key=lambda r: r[0])
        for _, cl, tl in results:
            seen = set(leg_lines); leg_lines += [l for l in cl if not (l in seen or seen.add(l))]; timeline += tl     # identical card lines across rooms: once
    # grading room(s): skeptic → proof in one room (WAVES=1) or concurrently (WAVES=2)
    graders = [["skeptic", "proof"]] if WAVES < 2 else [["skeptic", "proof"], ["proof", "skeptic"]]
    def grade(hs):
        rid = f"beezle-relay-{tag}-r{gi}g-{hs[0]}"; thread = f"relay-{tag}-r{gi}g-{hs[0]}"
        rid, thread = make_room(rid, f"RELAY {tag} r{gi} {hs[0]}", hs, thread)
        def build(pl_, rec_, leg_):
            b = brief(hs, hs if WAVES < 2 else hs[:1], paste(pl_, prior_hdr) + paste(leg_, LEG_CARDS_HDR), waves_skeptic=True, form_chain=chain)
            if WAVES >= 2: b = b.replace("≤ 25. End with: @proof", "≤ 25. End with: DONE").replace("The last lane ends with: @skeptic", "The last lane ends with: DONE")
            return b.replace(RECORD, rec_)
        text, _ = fit(build, pl, rec, list(leg_lines), label=f"[{hs[0]}] ")       # S-leg-brief-cap: run 46 died here — 68 lane cards pasted outside the priors
        evs, tl = run_room(rid, thread, hs, text, t0, stop_on=("proof" if WAVES < 2 else hs[0]), label=f"[{hs[0]}] ")
        disband(rid)
        return card_lines(evs), tl
    with ThreadPoolExecutor(max_workers=2) as ex:
        res = list(ex.map(grade, graders))
    graded = []
    for cl, tl in res:
        graded += cl; timeline += tl
    if WAVES >= 2:      # a grader that still @mentions the other repeats its lines in the 2nd room — drop exact duplicates
        seen = set(); graded = [l for l in graded if not (l in seen or seen.add(l))]
    lines_out += leg_lines + graded


ledger = {"tag": tag, "rooms": []}; T0 = time.time()
priors = [p for p in os.environ.get("ROOM_PRIORS", "").split(",") if p]      # resume: carry cards from already-finished rooms
START = int(os.environ.get("ROOM_START", "1"))                                   # resume: first room index to run
try:
    sweep_stale()                                                                 # S-relay-stale-room: a killed run's live rooms of this tag
    for gi, chain in enumerate(GROUPS, 1):
        if ONLY and gi not in ONLY:
            prev = f"{OUT}/{BASE}_relay_r{gi}_cards.md"
            if os.path.exists(prev): priors.append(prev)
            continue
        if gi < START: continue
        chain = room_fetch.fetch_chain(chain)                                     # S-fetch-bot: fetch FIRST when ROOM_FETCH=1
        handles = chain + ["skeptic", "proof"]
        # prior cards (S-10): gate-VERIFIED cards and GROUND+ seeds pass forward; only gate FAILURES are dropped — relay_priors.prior_lines
        pl = relay_priors.prior_lines(priors)
        if WAVES:
            t0 = time.time(); lines = []; timeline = []
            print(f"beezle-relay-{tag}-r{gi} WAVES={WAVES} waves {waves_of(chain)} max {WAVE_MAX} t0 {time.strftime('%H:%M:%S')}", flush=True)
            run_leg_waves(gi, chain, pl, lines, timeline, t0)
            wall = round(time.time() - t0)
        else:
            sfx = f"r{gi}" + (f"-rerun{int(time.time())%100000}" if ONLY else "")   # a rerun never reuses a disbanded room id
            rid = f"beezle-relay-{tag}-{sfx}"; thread = f"relay-{tag}-{sfx}"
            rid, thread = make_room(rid, f"RELAY {tag} r{gi}", handles, thread)
            def build(pl, rec):
                pt = ("\n==== PRIOR CARDS from earlier rooms of this relay (settled — build on them, never redo) ====\n" + "\n".join(pl)) if pl else ""
                return brief(chain, handles, pt).replace(RECORD, rec)
            text, pl = fit(build, pl, RECORD)
            t0 = time.time()
            evs, timeline = run_room(rid, thread, handles, text, t0, stop_on="proof")
            wall = round(time.time() - t0)
            lines = card_lines(evs)
        f = f"{OUT}/{tag}_relay_r{gi}_cards.md"
        open(f, "w").write(f"# RELAY cards ({tag} room {gi}: {','.join(chain)})\n" + "\n".join(lines) + "\n")
        tally = card_gate.gate(f, IN)
        ledger["rooms"].append({"room": gi, "lanes": chain, "wall_s": wall, "turns": len(timeline), "cards": len(lines), "prior_cards": sum(l.startswith("CARD:") for l in pl), "gate": tally, "file": f, "timeline": timeline})
        priors.append(f)
        print(f"  ROOM {gi} wall {wall}s turns {len(timeline)} cards {len(lines)} gate {tally}", flush=True)
        if not WAVES: disband(rid)
finally:
    for _rid in list(CREATED): disband(_rid)                                      # S-relay-stale-room: a kill never leaves live rooms; clean run → none left

ledger["wall_s"] = round(time.time() - T0)
json.dump(ledger, open(f"{OUT}/{tag}_relay.json", "w"), indent=1)
print("LEDGER", json.dumps({"tag": tag, "wall_s": ledger["wall_s"], "rooms": [{k: v for k, v in r.items() if k != "timeline"} for r in ledger["rooms"]]}), flush=True)
