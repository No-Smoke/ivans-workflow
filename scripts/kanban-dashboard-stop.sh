#!/bin/bash
# IWO Kanban Dashboard — stop
PIDFILE="/tmp/kanban-dashboard.pid"
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        notify-send "IWO Kanban" "Dashboard stopped (PID $PID)"
        exit 0
    fi
    rm -f "$PIDFILE"
fi
# Fallback: kill by process match
pkill -f "kanban-dashboard.py" 2>/dev/null && \
    notify-send "IWO Kanban" "Dashboard stopped" || \
    notify-send "IWO Kanban" "Not running"
