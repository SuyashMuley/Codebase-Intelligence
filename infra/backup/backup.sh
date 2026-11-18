#!/usr/bin/env bash
# Back up the application database to one file, and check the file can be read back.
#
#   DATABASE_URL=postgresql://user:pass@host:5432/dbname infra/backup/backup.sh /safe/place/cip.dump
#
# Needs the PostgreSQL 16 client tools (pg_dump, pg_restore). Use the DIRECT connection string on
# Neon (not the pooled one). The dump holds everything, including private source code and the
# encrypted provider keys: store it as carefully as the database itself, and encrypt it (for example
# with `age` or `gpg`) before it leaves a machine you control. It does NOT contain
# TOKEN_ENCRYPTION_KEY: back that up separately, or every stored key becomes unreadable on restore.
# NOTE: this script has never been run end to end. Test it on a copy before relying on it.
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL to the database to back up}"
OUT="${1:?usage: backup.sh OUTPUT_FILE}"

pg_dump --format=custom --no-owner --no-privileges --file "$OUT" "$DATABASE_URL"

# An unreadable archive is worse than none: find out now, not during an emergency.
pg_restore --list "$OUT" > /dev/null

sha256sum "$OUT" > "$OUT.sha256"
echo "backup written: $OUT ($(wc -c < "$OUT") bytes), checksum in $OUT.sha256"
