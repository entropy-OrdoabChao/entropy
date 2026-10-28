# CHECK — case_manager

PASS = (1) a slice request for 'claim 12' returns only the facts/timeline/record lines about it; (2) a bot result without a skeptic stamp never reaches CASE_FACTS; (3) every docket entry in the five active cases yields its deadlines within one cycle (today: 3 tracked, 0 for Filer/Lippi/Amerisave/State Farm = FAIL baseline); (4) each e-file receipt's exhibits reconciled against the docket.

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots case_manager`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
