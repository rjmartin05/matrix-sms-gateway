#!/bin/sh
# Consistent SQLite snapshot of the gateway DB (safe while the bridge runs; uses SQLite's backup API).
# Usage: scripts/backup.sh /path/to/backup/dir     (hook into the nightly backup job)
set -eu
dest="${1:?usage: backup.sh DEST_DIR}"
mkdir -p "$dest"
docker exec matrix-sms-gateway python -c "import sqlite3; s=sqlite3.connect('/data/smsgw.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
mv /matrix/sms-gateway/data/backup.db "$dest/smsgw-$(date +%F).db"
# Config + registration. registration.yaml holds the appservice tokens: keep backups private.
cp /matrix/sms-gateway/data/config.yaml /matrix/sms-gateway/registration.yaml "$dest/"
