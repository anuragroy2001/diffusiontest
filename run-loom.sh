#!/usr/bin/env bash
# The Loom, kept alive in a detached tmux session so it survives shell exits, SSH
# disconnects, and agent restarts.
#   ./run-loom.sh                -> start (or attach to) the tmux session
#   tmux attach -t loom          -> reattach later
#   tmux capture-pane -pt loom   -> peek at recent log lines without attaching
#   tmux kill-session -t loom    -> stop it
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is not installed (https://docs.astral.sh/uv/getting-started/installation/)." >&2
  exit 1
fi

SESSION="loom"
PORT="${LOOM_PORT:-8082}"
# server.py shells out to `tailscale ip -4`, which isn't on PATH in every shell even when
# the Tailscale app is installed. Fall back to this machine's LAN IP so the loom still binds.
export LOOM_HOST="${LOOM_HOST:-$(tailscale ip -4 2>/dev/null | head -1)}"
if [[ -z "$LOOM_HOST" ]]; then
  LOOM_HOST="$(ipconfig getifaddr en0 2>/dev/null || true)"
fi
if [[ -z "$LOOM_HOST" ]]; then
  echo "ERROR: could not determine a bind address (no tailscale, no en0). Set LOOM_HOST explicitly." >&2
  exit 1
fi
export LOOM_HOST

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[run-loom] session '$SESSION' already running -- attaching"
  exec tmux attach -t "$SESSION"
fi

if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $PORT is already in use outside tmux." >&2
  echo "       find it with: lsof -iTCP:$PORT -sTCP:LISTEN" >&2
  exit 1
fi

# Sync out here, not inside tmux -- a resolution failure (no network, a bad pyproject.toml) should
# fail loudly in this shell, not sit silently inside a detached pane nobody's attached to yet.
echo "[run-loom] syncing dependencies (uv sync)..."
uv sync

tmux new-session -d -s "$SESSION" -n loom -e "LOOM_HOST=$LOOM_HOST" "uv run python3 loom/server.py"
echo "[run-loom] started in tmux session '$SESSION' on port $PORT"
echo "[run-loom]   attach  ->  tmux attach -t $SESSION"
echo "[run-loom]   logs    ->  tmux capture-pane -pt $SESSION"
echo "[run-loom]   stop    ->  tmux kill-session -t $SESSION"
