#!/bin/sh
# Postgres runs this only when it creates a new, empty data directory (for example after
# the pgdata volume was removed). It loads the newest dump from ./backups, so the app
# comes back with its data instead of an empty database.
# The image sources this file, so it must not call exit.

latest=$(ls -1 /backups/"$POSTGRES_DB"-*.dump 2>/dev/null | sort | tail -n 1)
if [ -n "$latest" ]; then
    echo "db-init-restore: restoring $(basename "$latest")"
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-owner --no-privileges \
        --exit-on-error "$latest" \
        && echo "db-init-restore: done" \
        || echo "db-init-restore: FAILED, starting with what was restored" >&2
else
    echo "db-init-restore: no backups in /backups, starting with an empty database"
fi
