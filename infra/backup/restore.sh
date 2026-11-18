#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database.
#
#   TARGET_DATABASE_URL=postgresql://user:pass@host:5432/emptydb infra/backup/restore.sh cip.dump
#
# The target database must exist, be empty, and be able to create the `vector` extension (the
# pgvector image, or Neon, can). The restore runs in one transaction and stops at the first error,
# so a bad backup leaves the target empty instead of half full.
#
# After it succeeds:
#   1. Point the API at the restored database, with the SAME TOKEN_ENCRYPTION_KEY as before
#      (a different key cannot read stored provider keys; users would have to enter them again).
#   2. Run `alembic upgrade head` if the backup is older than the code (it is safe on a current one).
#   3. Read docs/RUNBOOK.md: a restore brings back whatever was deleted after the backup was taken.
# NOTE: this script has never been run end to end. Test it on a copy before relying on it.
set -euo pipefail

: "${TARGET_DATABASE_URL:?set TARGET_DATABASE_URL to an empty database}"
DUMP="${1:?usage: restore.sh DUMP_FILE}"

if [ -f "$DUMP.sha256" ]; then
  sha256sum --check "$DUMP.sha256"
else
  echo "warning: no checksum file next to the dump; its integrity is unchecked" >&2
fi

pg_restore --list "$DUMP" > /dev/null

pg_restore --no-owner --no-privileges --single-transaction --exit-on-error \
  --dbname "$TARGET_DATABASE_URL" "$DUMP"
echo "restore finished"
