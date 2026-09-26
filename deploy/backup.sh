#!/usr/bin/env bash
# Nightly backup. Run as the service user by myproject-backup.timer; by hand:
#   sudo systemctl start myproject-backup.service && journalctl -u myproject-backup -n 3
#
# Two things here are worth anything: the database and the uploads. The
# database is dumped in the custom format, so one table can be restored on its
# own, and 7 nightly + 4 weekly dumps are kept. The uploads are not copied on
# this disk — a nightly tarball of a growing media folder is how a disk fills.
# Both go off the server with rclone, the media folder synced incrementally.
#
# A backup nobody has restored is a hope. Restore one into a scratch database
# once, and write down how long it took.
set -euo pipefail

APP=/srv/myproject/app
BACKUPS=/srv/myproject/backups
# An rclone remote — and make it a CRYPT one. The dump holds every user's
# email, password hash and personal data, which a plain remote leaves readable
# on somebody else's disk. Nothing else in this script changes.
#
#   rclone config create backups-crypt crypt remote=b2:my-bucket \
#       password=<generated> filename_encryption=standard
REMOTE=remote:myproject-backups
KEEP_DAILY=7
KEEP_WEEKLY=4

# Not `source`d: the file holds values such as `MyProject <hello@…>` that bash
# would read as a redirection.
env_value() { grep -E "^$1=" "$APP/backend/.env" | tail -n 1 | cut -d= -f2-; }

DB_NAME=$(env_value DB_NAME)
DB_USER=$(env_value DB_USER)
DB_HOST=$(env_value DB_HOST); DB_HOST=${DB_HOST:-localhost}
DB_PORT=$(env_value DB_PORT); DB_PORT=${DB_PORT:-5432}
MEDIA_ROOT=$(env_value MEDIA_ROOT); MEDIA_ROOT=${MEDIA_ROOT:-$APP/backend/media}
export PGPASSWORD; PGPASSWORD=$(env_value DB_PASSWORD)

mkdir -p "$BACKUPS"
chmod 700 "$BACKUPS"

stamp=$(date +%F)
dump="$BACKUPS/db-$stamp.dump"

# Written to a temp name and renamed, so a half-finished dump is never taken
# for a good one. --no-owner/--no-acl: restorable under any role.
pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -Fc --no-owner --no-acl "$DB_NAME" > "$dump.part"
mv "$dump.part" "$dump"

# Sunday's dump is also kept as that week's.
if [ "$(date +%u)" = 7 ]; then
    cp "$dump" "$BACKUPS/weekly-$stamp.dump"
fi

# Keep the newest N of each kind. `find`, not `ls`: with no match ls exits 2,
# which under `set -e -o pipefail` ends the run on the very first night.
prune() {
    find "$BACKUPS" -maxdepth 1 -name "$1" -printf '%T@ %p\n' \
        | sort -rn | tail -n +"$(($2 + 1))" | cut -d' ' -f2- | xargs -r rm --
}
prune 'db-*.dump' "$KEEP_DAILY"
prune 'weekly-*.dump' "$KEEP_WEEKLY"

# Off the server: a backup on the same disk protects against a mistake, not
# against losing the machine. `sync` makes the remote match, so the pruning
# above bounds it too.
rclone sync "$BACKUPS" "$REMOTE/db" --quiet
rclone sync "$MEDIA_ROOT" "$REMOTE/media" --quiet

echo "✓ backup $stamp: db $(du -h "$dump" | cut -f1), media $(find "$MEDIA_ROOT" -type f | wc -l) files, $(ls "$BACKUPS" | wc -l) dumps kept"
