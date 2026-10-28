#!/usr/bin/env bash
# phase1_backup.sh — Phase 1 Guard 1 (MANDATORY pre-flight)
#
# Backs up the two surfaces Phase 1 can touch:
#   1. kanban.db        — won't be modified, but it lives next to beezle_structure.db
#   2. Chroma metadata  — 1k sample matching has_citation=true (the targeted backfill set)
#
# Snapshots only; embeddings excluded (would be 170GB).
# Run before any Phase 1 script with write flags.
#
# Output:
#   /home/{{USER_FIRST}}/.hermes/kanban.db.pre_phase1_<date>.bak
#   /home/{{USER_FIRST}}/.hermes/backups/pre_phase1/sample_metadata.json
#   /home/{{USER_FIRST}}/.hermes/backups/pre_phase1/sample_ids.txt
set -euo pipefail

DATE_TAG=$(date +%Y%m%d_%H%M%S)
HERMES_HOME=/home/{{USER_FIRST}}/.hermes
BACKUP_DIR=$HERMES_HOME/backups/pre_phase1
mkdir -p "$BACKUP_DIR"

echo "============================================================"
echo " Phase 1 Guard 1: Backup"
echo "============================================================"

# 1. kanban.db live backup via SQLite .backup command (consistent snapshot)
KANBAN_BAK=$HERMES_HOME/kanban.db.pre_phase1_${DATE_TAG}.bak
echo
echo "[1/2] kanban.db snapshot ..."
sqlite3 "$HERMES_HOME/kanban.db" ".backup '$KANBAN_BAK'"
ls -la "$KANBAN_BAK"
sqlite3 "$KANBAN_BAK" "PRAGMA integrity_check;" | head -3

# 2. ChromaDB sample metadata (1k chunks with has_citation=true; the backfill target set)
echo
echo "[2/2] Chroma metadata sample (has_citation=true, n=1000) ..."
source /home/{{USER_FIRST}}/.hermes/hermes-agent/venv/bin/activate
python3 - <<PY
import chromadb, json, sys
client = chromadb.HttpClient(host='localhost', port=8100)
coll = client.get_collection('legal_docs')

# Try multiple boolean representations — Chroma stores bool-as-string here
for val in ('True', 'true', True):
    try:
        sample = coll.get(where={'has_citation': val}, limit=1000,
                          include=['metadatas', 'documents'])
        if sample.get('ids'):
            print(f'  matched has_citation={val!r}: {len(sample["ids"])} chunks')
            break
    except Exception as e:
        print(f'  has_citation={val!r} failed: {e}')
        sample = {'ids': [], 'metadatas': [], 'documents': []}
else:
    sys.exit('ERROR: no chunks matched has_citation in any form')

# Write metadata + a separate ID list for fast diff later
out_meta = '$BACKUP_DIR/sample_metadata.json'
out_ids  = '$BACKUP_DIR/sample_ids.txt'
with open(out_meta, 'w') as f:
    json.dump({
        'backed_up_at': '${DATE_TAG}',
        'count': len(sample['ids']),
        'ids': sample['ids'],
        'metadatas': sample['metadatas'],
        # document text optional; metadata is the diff surface
        'documents_first_300chars': [d[:300] if d else '' for d in sample.get('documents', [])],
    }, f)
with open(out_ids, 'w') as f:
    for cid in sample['ids']:
        f.write(cid + '\n')
print(f'  wrote metadata: {out_meta}')
print(f'  wrote id list:  {out_ids}')
PY

# Summary
echo
echo "============================================================"
echo " BACKUP COMPLETE — Phase 1 writes may proceed"
echo "============================================================"
ls -la "$KANBAN_BAK" "$BACKUP_DIR/" 2>/dev/null
echo
echo "Restore commands if needed:"
echo "  cp '$KANBAN_BAK' '$HERMES_HOME/kanban.db'"
echo "  (Chroma metadata: use phase1_restore_sample.py with $BACKUP_DIR/sample_metadata.json)"
