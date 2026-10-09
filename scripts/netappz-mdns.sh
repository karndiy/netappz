#!/bin/bash
# Announces netappz.local on the LAN (Bonjour/mDNS), pointing at this Mac's current IP.
# Usage: scripts/netappz-mdns.sh [name]   (keeps running; follows IP changes)
NAME="${1:-netappz}"

current_ip() {
  local interface
  interface=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
  ipconfig getifaddr "${interface:-en0}" 2>/dev/null
}

trap 'kill "$announcer" 2>/dev/null; exit 0' TERM INT
announced=""
announcer=""
while true; do
  ip=$(current_ip)
  if [ "$ip" != "$announced" ] || ! kill -0 "$announcer" 2>/dev/null; then
    [ -n "$announcer" ] && kill "$announcer" 2>/dev/null
    announcer=""
    announced="$ip"
    if [ -n "$ip" ]; then
      echo "$(date '+%F %T') announcing $NAME.local -> $ip"
      dns-sd -P "Network Files" _http._tcp local 80 "$NAME.local" "$ip" path=/ >/dev/null &
      announcer=$!
    fi
  fi
  sleep 30
done
