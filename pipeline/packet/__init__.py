"""pipeline.packet — the filing packet tail after m1 (case-agnostic; every case path is an argument).

Promoted 2026-10-25 from the three working case-local scripts in
cases/example_v_insurer/drafts/_bundle13_order17/motions/vacate_m2room/
(_fetch_gmail_exhibits.py, _build_exhibits.py, _assemble_packet.py — kept there untouched). Logic unchanged;
the hardcoded exhibit tables are replaced by the stub's NEW EVIDENCE BLOCK rows (letter / title / [doc:<file>]),
read by pipeline.packet.manifest.

  fetch_exhibits   Gmail ids named in a row's **Source:** -> cleaned text exhibit at the row's [doc:*.txt]
  build_exhibits   record exhibits as PDFs beside the stub (copies, txt->pdf, ledger table, Pa.R.C.P. 1033 CLEAN + COMPARISON)
  assemble_packet  docx->pdf the motion, numbered PACKET/ + 99_PACKET_INDEX.md with page counts
"""
