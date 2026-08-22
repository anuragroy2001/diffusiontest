#!/usr/bin/env bash
# Run the DiffusionGemma UI in a detached tmux window so it survives this shell.
#   ./start-ui.sh          -> start (or restart) the window, print the URL
#   ./start-ui.sh stop     -> kill the window
#   ./start-ui.sh attach   -> attach to it
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${DG_SESSION:-diffusiongemma}"
WINDOW="${DG_WINDOW:-dg-ui}"
UI_PORT="${UI_PORT:-8081}"
TARGET="$SESSION:$WINDOW"

command -v tmux >/dev/null || { echo "ERROR: tmux is not installed." >&2; exit 1; }

kill_window() {
  if tmux has-session -t "$SESSION" 2>/dev/null && tmux list-windows -t "$SESSION" -F '#W' | grep -qx "$WINDOW"; then
    tmux kill-window -t "$TARGET"
    echo "killed $TARGET"
  else
    echo "no such window: $TARGET"
  fi
}

case "${1:-start}" in
  stop)   kill_window; exit 0 ;;
  attach) exec tmux attach -t "$TARGET" ;;
  start)  ;;
  *)      echo "usage: $0 [start|stop|attach]" >&2; exit 2 ;;
esac

# Restart cleanly: drop any previous window, then free the port if it's still held.
tmux has-session -t "$SESSION" 2>/dev/null \
  && tmux list-windows -t "$SESSION" -F '#W' | grep -qx "$WINDOW" \
  && tmux kill-window -t "$TARGET" && sleep 0.5 || true

if ss -tln "sport = :$UI_PORT" | grep -q LISTEN; then
  echo "ERROR: port $UI_PORT is already in use by something outside tmux:" >&2
  ss -ltnp "sport = :$UI_PORT" >&2
  exit 1
fi

CMD="UI_PORT=$UI_PORT '$DIR/serve.sh'"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-window -d -t "$SESSION" -n "$WINDOW" -c "$DIR" "$CMD"
else
  tmux new-session -d -s "$SESSION" -n "$WINDOW" -c "$DIR" "$CMD"
fi

sleep 1
if ! tmux list-windows -t "$SESSION" -F '#W' | grep -qx "$WINDOW"; then
  echo "ERROR: the UI window exited immediately. Run $DIR/serve.sh directly to see why." >&2
  exit 1
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || echo '<tailscale-ip>')"
echo
echo "UI running in tmux window '$TARGET'"
echo "  open     ->  http://${TS_IP}:${UI_PORT}/"
echo "  MagicDNS ->  http://thor2.tail7ae2b0.ts.net:${UI_PORT}/"
echo "  attach   ->  tmux attach -t $TARGET      (detach: Ctrl-b d)"
echo "  stop     ->  $0 stop"
