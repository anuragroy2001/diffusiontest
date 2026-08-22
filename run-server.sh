#!/usr/bin/env bash
# DiffusionGemma 26B-A4B as an OpenAI-compatible API, reachable over Tailscale only.
#   ./run-server.sh            -> bind this node's Tailscale IP, port 8080
#   DG_PORT=9000 ./run-server.sh
set -euo pipefail

# Resolve the Tailscale IP at launch so a DHCP/tailnet change can't silently
# turn this into a 0.0.0.0 listener.
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [[ -z "$TS_IP" ]]; then
  echo "ERROR: no Tailscale IPv4 found. Is tailscaled up? (tailscale status)" >&2
  exit 1
fi

export DG_HOST="${DG_HOST:-$TS_IP}"
export DG_PORT="${DG_PORT:-8080}"
export NGL="${NGL:-99}"

if ss -tln "sport = :$DG_PORT" | grep -q LISTEN; then
  echo "ERROR: port $DG_PORT is already in use on this host." >&2
  exit 1
fi

echo "DiffusionGemma  ->  http://${DG_HOST}:${DG_PORT}/v1"
echo "MagicDNS        ->  http://thor2.tail7ae2b0.ts.net:${DG_PORT}/v1"
echo "backend log     ->  ~/diffusiongemma/backend.log"
echo
exec python3 ~/diffusiongemma/dg_openai_server.py
