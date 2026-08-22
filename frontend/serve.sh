#!/usr/bin/env bash
# Static UI for the DiffusionGemma API. Tailscale-only, same policy as run-server.sh.
#   ./serve.sh              -> http://<tailscale-ip>:8081
#   UI_PORT=9001 ./serve.sh
set -euo pipefail

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [[ -z "$TS_IP" ]]; then
  echo "ERROR: no Tailscale IPv4 found. Is tailscaled up? (tailscale status)" >&2
  exit 1
fi

UI_HOST="${UI_HOST:-$TS_IP}"
UI_PORT="${UI_PORT:-8081}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ss -tln "sport = :$UI_PORT" | grep -q LISTEN; then
  echo "ERROR: port $UI_PORT is already in use on this host." >&2
  exit 1
fi

echo "UI       ->  http://${UI_HOST}:${UI_PORT}/"
echo "MagicDNS ->  http://thor2.tail7ae2b0.ts.net:${UI_PORT}/"
echo "(the page talks to the API on port 8080; change it in the header field if needed)"
echo

exec python3 -m http.server "$UI_PORT" --bind "$UI_HOST" --directory "$DIR"
