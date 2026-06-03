#!/usr/bin/env bash
# Obelyth chain-state backup
# Runs from cron every 6 hours. Snapshots /var/lib/obelyth/chain_state.json
# to /var/lib/obelyth/backups/ with a timestamp. Rotates files older than 14 days.
#
# Install:
#   cp deploy/backup.sh /usr/local/bin/obelyth-backup.sh
#   chmod +x /usr/local/bin/obelyth-backup.sh
#   echo '0 */6 * * * root /usr/local/bin/obelyth-backup.sh' >> /etc/crontab

set -euo pipefail

DATA_DIR="/var/lib/obelyth"
BACKUP_DIR="${DATA_DIR}/backups"
SOURCE="${DATA_DIR}/chain_state.json"
KEEP_DAYS=14

mkdir -p "${BACKUP_DIR}"

if [[ ! -f "${SOURCE}" ]]; then
  echo "[obelyth-backup] No chain_state.json yet — skipping."
  exit 0
fi

TS=$(date -u +%Y%m%d_%H%M%S)
DEST="${BACKUP_DIR}/chain_state_${TS}.json"

cp "${SOURCE}" "${DEST}"
echo "[obelyth-backup] $(date -u --iso-8601=seconds) backup → ${DEST} ($(du -h "${DEST}" | cut -f1))"

# Rotate — delete files older than KEEP_DAYS
find "${BACKUP_DIR}" -name 'chain_state_*.json' -mtime "+${KEEP_DAYS}" -delete
