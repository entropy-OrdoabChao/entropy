#!/usr/bin/env python3
"""make_admin_bots — the ADMIN / REGULATOR ROOM bot folders (pipeline/m2/bots/admin/<name>/) from ONE spec, in the exact shape
make_bots.SPEC produces (JOB.yaml data · SOUL.md contract · CHECK.md acceptance). make_bots.py is NOT edited: this module
imports its job_yaml / soul_md / check_md / write so the folder shape stays byte-compatible (dispatch_prompt.handoff_consumes
reads bots/*/*/JOB.yaml `handoff`; bot name actor_map → room lane actor-map via dispatch_prompt.bot_lane).
    python3 -m pipeline.m2.make_admin_bots            # write (never clobbers a human-edited SOUL/CHECK: writes .new beside it)
    python3 -m pipeline.m2.make_admin_bots --list     # roster only
Lane SOULs mirror obsidian_sync/00_system/rooms/prompts/admin/<lane>-SOUL.md (the profile copy is seeded by
scripts/rooms/admin_seed_profiles.sh)."""
from __future__ import annotations
import sys
from pathlib import Path

from pipeline.m2 import make_bots as MB

BOTS = MB.BOTS   # pipeline/m2/bots

# name, group, unit, axis line, families (source-key prefixes), extra queries, SOUL body, CHECK
ADMIN_SPEC = [
 ("actor_map", "admin", "actor", "ADMIN 1 — ACTOR MAP: every agency/company in the matter → its regulator, statutory complaint channel, statutory records channel (escalation_ladder + corpus).",
  ["usc_15_1681", "usc_12_2605", "usc_12_5481", "usc_5_552", "usc_5_552a", "pa_65ps_67", "pa_40ps", "pa_73ps_201", "cfr_12_1024", "cfr_12_1002"],
  ["{claim} consumer reporting agency means", "{claim} furnisher of information", "{claim} servicer means", "{claim} agency means 5 U.S.C. 551", "complaint to the Bureau 12 U.S.C. 5493", "Right-to-Know Law agency record"],
  """Unit = one ACTOR. Call escalation_ladder for the actor/forum FIRST, then definition_lookup for its capacity term (CRA §1681a(f),
furnisher, servicer §2605(i)(2), creditor §1691a(e), agency §551(1)). One card per actor: ROLE, REGULATOR (+ the statute giving it
power), COMPLAINT CHANNEL, RECORDS CHANNEL (FOIA / Privacy Act / RTKL / §1681g / none), LADDER key, AUTHORITY chunk. An actor with
no ladder is 'NOT IN LADDER'; a regulator you cannot quote is NOT IN CORPUS. Capacity follows the statute's definition, never the
party's self-label.""",
  "PASS = on fed:2:26-cv-00123 every DEFENDANT POSTURES row (Avant, Experian, Equifax, TransUnion, GreenSky, Example Card Issuer) + CFPB/FTC carries a card with ROLE + REGULATOR quoted; skeptic 0 REJECTED; NOT IN CORPUS flagged, not guessed."),
 ("records_request", "admin", "actor", "ADMIN 2 — RECORDS REQUEST: FOIA 5 U.S.C. §552 / Privacy Act §552a / PA RTKL 65 P.S. §67 / FCRA §1681g — statute, custodian, fee rule, response clock, on-lapse, mail mode; verbatim.",
  ["usc_5_552", "usc_5_552a", "pa_65ps_67", "usc_15_1681g", "usc_15_1681j", "cfr_12_1070", "cfr_24_15", "cfr_16_4"],
  ["{claim} reasonably describes such records", "twenty days excepting Saturdays Sundays legal public holidays", "{claim} access to his record", "five business days Right-to-Know", "{claim} clearly and accurately disclose all information in the consumer's file", "fee waiver public interest"],
  """Unit = one ACTOR with a records channel. One card per request: STATUTE, RULE (verbatim shall words), FEE RULE, RESPONSE CLOCK
(working vs calendar days, from receipt), ON LAPSE (constructive denial / deemed denied / private right), MAIL (LetterStream
first-class default; certified+ERR only where the statute or custodian rule requires proof of receipt — quote it).""",
  "PASS = one card per actor with a records channel; every clock quoted from the corpus; no certified-mail line without the requiring words; skeptic 0 REJECTED."),
 ("formal_demand", "admin", "actor", "ADMIN 3 — FORMAL DEMAND: the governing-statute demand — FCRA §1681i/§1681s-2, RESPA §2605(e) + Reg X 1024.35/.36, ECOA §1691(d) + Reg B 1002.9, PA UTPCPL 73 P.S. §201, PA Ins. Dept 40 P.S. — duty, trigger, clock, on-lapse liability.",
  ["usc_15_1681i", "usc_15_1681s", "usc_15_1681n", "usc_15_1681o", "usc_12_2605", "cfr_12_1024", "usc_15_1691", "cfr_12_1002", "pa_73ps_201", "pa_40ps", "cfr_12_1022"],
  ["{claim} reasonable reinvestigation 30-day period", "{claim} duties of furnishers upon notice of dispute", "qualified written request acknowledge 5 days respond 30 days", "notice of error servicer shall", "statement of specific reasons adverse action 30 days", "unfair or deceptive acts or practices treble damages", "unfair insurance practices complaint Department"],
  """Unit = one ACTOR with a governing statute. One card per demand: DUTY (verbatim shall words + chunk), TRIGGER (what the demand must
contain, addressee), CLOCK (days from receipt, verbatim), ON LAPSE (the liability section, verbatim), RECORD (the fact of record the
demand rests on). Hub pins: rules/regx-1024-35-36-demands.md, rules/ecoa-regb-denial.md. Capacity decides which demand (CRA vs
furnisher vs servicer), never the label.""",
  "PASS = each FCRA defendant has a §1681i (CRA) or §1681s-2 (furnisher) demand card with the 30-day words quoted; skeptic 0 REJECTED."),
 ("deadline_tracker", "admin", "clock", "ADMIN 4 — DEADLINE TRACKER: every clock the room opened → start event, verbatim rule, due date via deadline_compute_all/deadline_track, mail mode, status.",
  ["usc_5_552", "usc_15_1681i", "usc_12_2605", "pa_65ps_67", "frcp_rule_6", "usc_15_1681p"],
  ["{claim} days after receipt", "excepting Saturdays Sundays and legal public holidays", "{claim} business days", "computing time period"],
  """Unit = one CLOCK (from a records-request / formal-demand / actor-map card). Call deadline_compute_all / deadline_track for every
dated start; an UNDATED clock (starts on receipt) is posted with 'receipt + n days', never an invented date. MAIL: LetterStream
first-class default; certified+ERR only where required (quote). STATUS running | lapsed | not started.""",
  "PASS = one card per clock clause in the earlier lanes' cards; zero invented dates (every DUE traces to a record date or 'receipt + n'); tracked: yes for every dated clock."),
 ("failure_escalation", "admin", "clock", "ADMIN 5 — FAILURE ESCALATION: on a lapsed/non-responsive clock → the claim (statutory private right / mandamus §1361 / APA §706 / FOIA §552(a)(4)(B) / state tort), elements vs record, vehicle, forum; handed to the order room + m1 as CARD + GROUND+.",
  ["usc_28_1361", "usc_5_706", "usc_5_702", "usc_5_704", "usc_5_552", "usc_15_1681n", "usc_15_1681o", "usc_15_1681p", "usc_12_2605", "usc_15_1691e", "pa_73ps_201", "pa_65ps_67", "usc_28_1331", "usc_28_1391", "usreports", "ca3_"],
  ["compel an officer or employee of the United States to perform a duty owed", "compel agency action unlawfully withheld or unreasonably delayed", "{claim} de novo agency bears the burden", "{claim} civil liability willful noncompliance", "venue civil action agency defendant 1391(e)"],
  """Unit = one lapsed (or running → OPEN) CLOCK from the deadline-tracker's cards. One card: CLAIM, ELEMENTS (each → record fact or
OPEN), VEHICLE (+ forum rule cite), FORUM (+ venue cite), RULE (verbatim + chunk), HANDOFF (order room | m1). Every authority
resolved is also posted as a GROUND+ line so the receiving room gets the ground, not a pointer. A running clock never yields a
SUSTAINED card.""",
  "PASS = every lapsed clock has a CLAIM card with all elements mapped; mandamus/APA cards quote §1361/§706 words; GROUND+ posted per authority; skeptic 0 REJECTED."),
]


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if "--list" in argv:
        for s in ADMIN_SPEC: print(f"{s[1]:<9} {s[0]:<22} unit={s[2]:<9} {s[3][:80]}")
        print(f"{len(ADMIN_SPEC)} admin bots"); return 0
    for n in MB.write(ADMIN_SPEC, force="--force" in argv): print("wrote", n)
    print(f"{len(ADMIN_SPEC)} admin bots → {BOTS}/admin"); return 0


if __name__ == "__main__":
    sys.exit(main())
