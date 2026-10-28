# CHECK — authority_binding

PASS = every cite in the specimen gets court / binding-or-not / good-law / holding-quoted. Pair: Kelly 625 F.2d 486 (3d Cir, binding on EDPA, GROUNDED) vs an unpublished district opinion cited as controlling (GAP: persuasive).

FORUM pair: "28 U.S.C. § 1746; Fed. R. Civ. P. 52(a)" cited against a Phila. C.C.P. order (FORUM_MISMATCH -> Pa.R.A.P. 1925(a)(1)) vs
"Pa.R.A.P. 1925(a)(1)" against the same order (GROUNDED). "Liljeberg, 486 U.S. 847" against a PA judge = persuasive (statutory § 455 holding);
"Caperton, 556 U.S. 868" = binding (due process).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots authority_binding`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
