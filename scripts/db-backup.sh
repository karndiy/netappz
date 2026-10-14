#!/bin/sh
# Runs in the "backup" container: dumps the database into /backups (./backups on the host,
# outside Docker) when the container starts and then every BACKUP_INTERVAL_HOURS.
# Keeps the newest BACKUP_KEEP dumps. Restore with scripts/db-restore.sh.
set -u

interval=$(( ${BACKUP_INTERVAL_HOURS:-6} * 3600 ))
keep=${BACKUP_KEEP:-28}

trap 'exit 0' TERM INT

backup() {
    name="$PGDATABASE-$(date -u +%Y%m%d-%H%M%SZ).dump"
    # Write to a temporary name so a half-written file is never taken for a backup.
    if pg_dump --format=custom --file="/backups/.$name.partial" \
        && pg_restore --list "/backups/.$name.partial" > /dev/null; then
        mv "/backups/.$name.partial" "/backups/$name"
        echo "$(date -u +%FT%TZ) saved $name ($(du -h "/backups/$name" | cut -f1))"
        # Remove the oldest dumps beyond the newest $keep.
        ls -1 /backups/"$PGDATABASE"-*.dump | sort -r | tail -n +$((keep + 1)) | while read -r old; do
            rm -f "$old" && echo "removed $(basename "$old")"
        done
    else
        rm -f "/backups/.$name.partial"
        echo "$(date -u +%FT%TZ) backup FAILED" >&2
    fi
}

while true; do
    backup
    sleep "$interval" &
    wait $!
done
