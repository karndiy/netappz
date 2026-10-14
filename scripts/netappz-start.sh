#!/bin/sh
# Starts Network Files after the Mac boots and logs in: opens Docker Desktop, waits for
# the engine, then starts the stack. Run at login by local.netappz.start.plist.
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/.." || exit 1

echo "$(date '+%F %T') starting Docker"
open -g -a Docker
# Docker Desktop can take several minutes after a boot.
i=0
until docker info > /dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 120 ]; then
        echo "$(date '+%F %T') Docker did not start within 10 minutes" >&2
        exit 1
    fi
    sleep 5
done

echo "$(date '+%F %T') Docker ready, starting the stack"
docker-compose up -d
