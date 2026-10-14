#!/bin/sh
# Replaces the running database with a dump from ./backups.
#   scripts/db-restore.sh                 restore the newest dump
#   scripts/db-restore.sh backups/X.dump  restore a chosen dump
# The current data is saved first as backups/pre-restore-*.dump.
set -eu

cd "$(dirname "$0")/.."
user=${POSTGRES_USER:-notes}
db=${POSTGRES_DB:-notes}
file=${1:-$(ls -1 backups/"$db"-*.dump 2>/dev/null | sort | tail -n 1)}

if [ -z "$file" ] || [ ! -f "$file" ]; then
    echo "No dump found. Usage: $0 [backups/<file>.dump]" >&2
    exit 1
fi

printf 'Replace database "%s" with %s? Type yes: ' "$db" "$file"
read -r answer
[ "$answer" = yes ] || { echo "Cancelled."; exit 1; }

safety="backups/pre-restore-$(date -u +%Y%m%d-%H%M%SZ).dump"
docker exec network-files-db pg_dump -U "$user" --format=custom "$db" > "$safety"
echo "Saved current data to $safety"

docker stop network-files network-files-telegram > /dev/null
docker exec network-files-db psql -U "$user" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE \"$db\" WITH (FORCE)" -c "CREATE DATABASE \"$db\" OWNER \"$user\""
docker exec -i network-files-db pg_restore -U "$user" -d "$db" --no-owner --no-privileges --exit-on-error < "$file"
docker start network-files network-files-telegram > /dev/null
echo "Restored $file. The app is starting again."
