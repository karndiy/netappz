#!/bin/sh
# Runs in the "backup" container: dumps the database into /backups (./backups on the host,
# outside Docker) when the container starts and then every BACKUP_INTERVAL_HOURS, keeping
# the newest BACKUP_KEEP dumps. Settings → System & backups can change both (saved in
# /backups/.schedule) and ask for a backup now (/backups/.backup-now). A failed backup leaves
# its error in /backups/.last-error for the system check. Restore with scripts/db-restore.sh.
set -u

trap 'exit 0' TERM INT

read_schedule() {
    hours=${BACKUP_INTERVAL_HOURS:-6}
    keep=${BACKUP_KEEP:-28}
    if [ -f /backups/.schedule ]; then
        # Only digits are taken from the file the web app writes.
        h=$(sed -n 's/^interval_hours=\([0-9]\{1,4\}\)$/\1/p' /backups/.schedule | head -n 1)
        k=$(sed -n 's/^keep=\([0-9]\{1,4\}\)$/\1/p' /backups/.schedule | head -n 1)
        [ -n "$h" ] && [ "$h" -ge 1 ] && hours=$h
        [ -n "$k" ] && [ "$k" -ge 1 ] && keep=$k
    fi
}

backup() {
    name="$PGDATABASE-$(date -u +%Y%m%d-%H%M%SZ).dump"
    # Write to a temporary name so a half-written file is never taken for a backup.
    if pg_dump --format=custom --file="/backups/.$name.partial" 2> /tmp/backup-error \
        && pg_restore --list "/backups/.$name.partial" > /dev/null 2>> /tmp/backup-error; then
        mv "/backups/.$name.partial" "/backups/$name"
        rm -f /backups/.last-error
        echo "$(date -u +%FT%TZ) saved $name ($(du -h "/backups/$name" | cut -f1))"
        # Remove the oldest dumps beyond the newest $keep.
        ls -1 /backups/"$PGDATABASE"-*.dump | sort -r | tail -n +$((keep + 1)) | while read -r old; do
            rm -f "$old" && echo "removed $(basename "$old")"
        done
    else
        rm -f "/backups/.$name.partial"
        { date -u '+%F %H:%M UTC:'; tail -n 3 /tmp/backup-error; } > /backups/.last-error
        echo "$(date -u +%FT%TZ) backup FAILED: $(tail -n 1 /tmp/backup-error)" >&2
    fi
}

last=0
while true; do
    read_schedule
    if [ -f /backups/.backup-now ] || [ $(($(date +%s) - last)) -ge $((hours * 3600)) ]; then
        rm -f /backups/.backup-now
        backup
        last=$(date +%s)
    fi
    sleep 30 &
    wait $!
done
