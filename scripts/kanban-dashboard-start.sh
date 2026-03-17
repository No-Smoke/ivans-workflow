#!/bin/bash
# IWO Kanban Dashboard — start/ensure running
# Launches the dashboard if not already running, then opens browser.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env
if [ -f "$IWO_ROOT/.env" ]; then
    set -a; source "$IWO_ROOT/.env"; set +a
fi

DASH_SCRIPT="$IWO_ROOT/tools/kanban-dashboard.py"
ICON="$IWO_ROOT/assets/kanban-icon-256.png"
PORT=8787
LOG="/tmp/kanban-dashboard.log"
PIDFILE="/tmp/kanban-dashboard.pid"

# Check if already running
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        xdg-open "http://localhost:$PORT" 2>/dev/null &
        notify-send -i "$ICON" "IWO Kanban" "Already running (PID $PID)"
        exit 0
    fi
    rm -f "$PIDFILE"
fi

# Also check by port
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
    xdg-open "http://localhost:$PORT" 2>/dev/null &
    notify-send -i "$ICON" "IWO Kanban" "Already running on port $PORT"
    exit 0
fi

# Start the dashboard
cd "$IWO_ROOT"
nohup python3 "$DASH_SCRIPT" --port "$PORT" > "$LOG" 2>&1 &
DASH_PID=$!
echo "$DASH_PID" > "$PIDFILE"

# Wait for it to be ready
for i in $(seq 1 20); do
    if curl -s -o /dev/null -w "" "http://localhost:$PORT/" 2>/dev/null; then
        xdg-open "http://localhost:$PORT" 2>/dev/null &
        notify-send -i "$ICON" "IWO Kanban" "Started (PID $DASH_PID)"
        exit 0
    fi
    sleep 0.25
done

notify-send -u critical "IWO Kanban" "Failed to start — check $LOG"
exit 1
