#!/usr/bin/env bash
# tests/regress_depth.sh — the depth layer must stay WIRED (2026-10-27: it was built in June/Aug and sat with zero callers).
# Three checks, all read-only, torch-free where possible:
#   1. connected_notes.graph_walk traverses via pipeline.obsidian_graph.obsidian_graph_walk (hop-1/2 notes > 0 for a linked seed)
#   2. ground_pack.deepen_section surfaces the cites a rule's OWN TEXT names (Pa.R.A.P. 1311 -> 312 / 341 / § 702) with resolved text
#   3. cards_to_cells grounds a verbatim forum-row quote as [official:<src>] when the chunk-DB does not hold it
# Exit non-zero on any failure. Run: bash tests/regress_depth.sh   (first run after a vault change rebuilds the wikilink index: minutes)
set -u
cd "$(dirname "$0")/.."
PY=${PY:-/usr/bin/python3}
I=${I:-obsidian_sync/cases/example_v_insurer/drafts/_bundle14_cert341c/room_inputs_m2room}
env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES PYTHONPATH=.:scripts/rooms "$PY" - "$I" <<'EOF' 2>&1 | grep -vE "^INFO|Warning|\[retrieval\]"
import sys, re, inspect
fails = []
# 1 — walk is the existing walker, and it walks
from pipeline import connected_notes as cn
src = inspect.getsource(cn.graph_walk)
if "obsidian_graph_walk" not in src: fails.append("graph_walk does not call obsidian_graph_walk (private BFS is the first-hit defect in code form)")
seeds = sorted(p.stem for p in cn.NOTES.glob("*.md") if "[[" in p.read_text(errors="ignore"))[:2]
w = cn.graph_walk(seeds, hops=2, max_nodes=30) if seeds else []
deep = sum(1 for _, h, _ in w if h > 0)
print(f"[1] walk seeds={len(seeds)} hop>0 notes={deep}")
if deep == 0: fails.append("graph_walk returned no hop-1/2 notes")
# 2 — DEEPEN reads the rule's own cross-references
import ground_pack as gp
body, _ = gp.resolve_rule("Pa.R.A.P. 1311", "")
d = gp.deepen_section([("Pa.R.A.P. 1311", body or "")], cap_per=8)
named = {x for x in ("Pa.R.A.P. 312", "Pa.R.A.P. 341", "42 Pa.C.S. § 702") if x in d}
print(f"[2] DEEPEN near 1311 names {sorted(named)}; rows={d.count(chr(10)+'- ')}")
if len(named) < 2: fails.append("DEEPEN did not surface 1311's own cross-references (312/341/§702)")
# 3 — official-row grounding
import cards_to_cells as c
n = c.load_pack_official(sys.argv[1])
q = "may modify or rescind any order within 30 days after its entry"
hit = c._pack_official_hit(q) if n else None
print(f"[3] pack official rows={n} § 5505 quote -> {hit}")
if n and not hit: fails.append("verbatim forum-row quote not grounded as official:<src>")
print("RESULT:", "GREEN" if not fails else "RED " + " | ".join(fails))
sys.exit(1 if fails else 0)
EOF
