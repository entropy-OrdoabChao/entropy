#!/usr/bin/env bash
# chain_packet_tail.sh — the filing-packet tail AFTER m1 (sourced by scripts/chain_order_m2room.sh; owned by pipeline/packet/).
# Expects the chain's env: SLUG B M COURT FTYPE (and optionally PY, L, ROOT). Each step is one module in pipeline/packet/,
# driven by the stub's '## NEW EVIDENCE BLOCK' rows (letter / title / [doc:<file>]) — $M/EXHIBITS.md if present, else the
# stub itself. Nothing here composes argument: fetch = record emails -> text exhibits; build = record files -> PDFs
# (incl. Pa.R.C.P. 1033 CLEAN + COMPARISON); assemble = numbered PACKET/ + 99_PACKET_INDEX.md with page counts.
#
# Usage (from the chain, after the m1 step):   source "$ROOT/scripts/chain_packet_tail.sh"; packet_tail
# Standalone:  SLUG=example_v_insurer B=<bundle> M=<motion dir> COURT=PA_CP_PHILA FTYPE=motion_to_vacate \
#              bash scripts/chain_packet_tail.sh
# Env knobs:   PACKET_LETTERS (Gmail letters to fetch; default D,E,G,I)  PACKET_DIR (default $M/PACKET)  PACKET_MANIFEST (manifest file; default $M/EXHIBITS.md)
#              PACKET_SKIP_FETCH=1 (offline / token missing)             PACKET_DRAFT_DOCX (default $M/${FTYPE}_M1_DRAFT.docx)
#              PACKET_MOTION_NAME / PACKET_MOTION_DESC / PACKET_INDEX_TITLE / PACKET_CAPTION / PACKET_TODO — assemble_packet
#              labels for a forum other than ORDER_17's defaults (run_screen.sh, CA3); empty = the module's defaults (unchanged).

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
packet_tail() {
  local ROOT="${ROOT:-<home>}" PY="${PY:-/usr/bin/python3}" L="${L:-/dev/null}"
  local CASE="$ROOT/obsidian_sync/cases/$SLUG"
  local STUB="${PACKET_MANIFEST:-$M/EXHIBITS.md}"; [ -f "$STUB" ] || STUB="$M/${FTYPE}.md"     # PACKET_MANIFEST: a stance-filtered manifest (run 53); default unchanged
  local DRAFT="${PACKET_DRAFT_DOCX:-$M/${FTYPE}_M1_DRAFT.docx}"
  local PDIR="${PACKET_DIR:-$M/PACKET}"
  _plog() { echo "[$(date +%H:%M:%S)] packet-tail: $*" | tee -a "$L"; }

  for v in SLUG B M COURT FTYPE; do [ -n "${!v}" ] || { _plog "STOP: $v unset"; return 1; }; done
  [ -f "$STUB" ] || { _plog "STOP: no exhibit manifest ($M/EXHIBITS.md or $M/${FTYPE}.md)"; return 1; }
  [ -f "$DRAFT" ] || { _plog "STOP: m1 draft .docx missing ($DRAFT) — m1 did not finish"; return 1; }
  _plog "manifest=$STUB draft=$DRAFT packet=$PDIR"
  # S-packet-attachments (class, 2026-11-13): the FILING SPEC decides whether a packet carries exhibits. A pleading (reply to new
  # matter, answer) attaches only writings it is BASED on (Pa.R.C.P. 1019(i)); the stub's NEW EVIDENCE BLOCK is built from timeline
  # cites and is a motion/brief concept — for a spec with 'attachments: none' it produced a 26-page reply packet carrying the
  # opponent's brief and a court order as Exhibits A/B. Spec key: attachments: none|manifest (default manifest = old behaviour).
  local SPEC_Y="$(readlink -f "$ROOT/pipeline")/../templates/filing_specs/${FTYPE}.yaml"   # same resolution as m1_spec_assembler.SPEC_DIR (code root may be a symlink)
  if [ -f "$SPEC_Y" ] && grep -qE '^attachments:[[:space:]]*none' "$SPEC_Y"; then
    _plog "spec ${FTYPE}: attachments: none — pleading filed alone (1019(i)); exhibit fetch/build skipped"
    mkdir -p "$PDIR"; rm -f "$PDIR"/[0-9][0-9]_EXHIBIT_* 2>/dev/null
    export PACKET_SKIP_FETCH=1 PACKET_NO_EXHIBITS=1
    # assemble_packet/efile_artifact read the manifest's NEW EVIDENCE BLOCK: hand them a copy with that block emptied (stub otherwise intact)
    STUB_NOEX="$M/EXHIBITS_none.md"
    awk 'BEGIN{skip=0} /^## NEW EVIDENCE BLOCK/{print; print ""; print "(none — attachments: none per filing spec; Pa.R.C.P. 1019(i))"; skip=1; next} /^## /{skip=0} skip==0{print}' "$STUB" > "$STUB_NOEX"
    STUB="$STUB_NOEX"
  fi

  cd "$ROOT" || return 1
  if [ "${PACKET_SKIP_FETCH:-0}" != "1" ]; then
    timeout 300 env PYTHONPATH="$ROOT" "$PY" -m pipeline.packet.fetch_exhibits --m "$M" --stub "$STUB" \
        --letters "${PACKET_LETTERS:-D,E,G,I}" 2>&1 | grep -v 'beezle-mcp' | tail -8 | tee -a "$L"
    [ "${PIPESTATUS[0]}" = "0" ] || { _plog "STOP: fetch_exhibits failed"; return 1; }
  else
    _plog "fetch_exhibits skipped (PACKET_SKIP_FETCH=1)"
  fi
  if [ "${PACKET_NO_EXHIBITS:-0}" != "1" ]; then
  timeout 600 env PYTHONPATH="$ROOT" "$PY" -m pipeline.packet.build_exhibits --root "$ROOT" --case "$CASE" --m "$M" --stub "$STUB" \
      2>&1 | grep -v '^INFO\|beezle-mcp' | tail -20 | tee -a "$L"
  [ "${PIPESTATUS[0]}" = "0" ] || { _plog "STOP: build_exhibits failed"; return 1; }
  fi
  local -a AARGS=()      # forum labels (optional knobs; absent = assemble_packet defaults, byte-identical for the order chain)
  [ -n "${PACKET_MOTION_NAME:-}" ] && AARGS+=(--motion-name "$PACKET_MOTION_NAME")
  [ -n "${PACKET_MOTION_DESC:-}" ] && AARGS+=(--motion-desc "$PACKET_MOTION_DESC")
  [ -n "${PACKET_INDEX_TITLE:-}" ] && AARGS+=(--index-title "$PACKET_INDEX_TITLE")
  [ -n "${PACKET_CAPTION:-}" ] && AARGS+=(--caption "$PACKET_CAPTION")
  [ -n "${PACKET_TODO:-}" ] && AARGS+=(--todo "$PACKET_TODO")
  timeout 600 env PYTHONPATH="$ROOT" "$PY" -m pipeline.packet.assemble_packet --m "$M" --stub "$STUB" --draft-docx "$DRAFT" \
      --packet-dir "$PDIR" "${AARGS[@]}" 2>&1 | grep -v '^INFO\|beezle-mcp' | tail -20 | tee -a "$L"
  [ "${PIPESTATUS[0]}" = "0" ] || { _plog "STOP: assemble_packet failed"; return 1; }
  # e-file artifact: chain_<name>/<stub>_FILING.pdf + EFILE_PART3.json — every page-3 value from data (registry judge,
  # units header, CASE_TIMELINE, filing-type deadline_days); never a literal (S-efile-part3-source).
  # efile_artifact --stub is the draft STEM (<stem>_DRAFT.md / <stem>_FILING.pdf), not the manifest path the three steps above take
  # (passing the path produced chain_<m>/obsidian_sync/.../x_FILING.pdf -> pdfunite 'Could not open file', 2026-11-13). Stem = the
  # *_DRAFT.md in $M (one per motion dir), else FTYPE.
  local STEM; STEM=$(ls "$M"/*_DRAFT.md 2>/dev/null | head -1 | xargs -r basename | sed 's/_DRAFT\.md$//'); STEM="${STEM:-$FTYPE}"
  timeout 300 env PYTHONPATH="$ROOT" "$PY" -m pipeline.packet.efile_artifact --root "$ROOT" --case "$CASE" --m "$M" --stub "$STEM" \
      --court "${COURT:-}" --ftype "${FTYPE:-}" 2>&1 | grep -v '^INFO\|beezle-mcp' | tail -6 | tee -a "$L"
  [ "${PIPESTATUS[0]}" = "0" ] || { _plog "STOP: efile_artifact failed"; return 1; }
  _plog "DONE -> $PDIR/99_PACKET_INDEX.md (NOT e-filed; cart/submit is the user's)"
}

# run directly (not sourced) -> execute
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  packet_tail
fi
