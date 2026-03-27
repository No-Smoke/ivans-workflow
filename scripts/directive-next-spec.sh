#!/bin/bash
# IWO Directive: Plan Next Spec
# Called from desktop launcher right-click menu
# v3: env-driven paths, .env auto-load

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if IWO_PROJECT_ROOT not already set
if [ -z "$IWO_PROJECT_ROOT" ] && [ -f "$IWO_ROOT/.env" ]; then
    set -a; source "$IWO_ROOT/.env"; set +a
fi

LOGFILE="/tmp/iwo-directive-debug.log"
echo "$(date -Iseconds) directive-next-spec.sh STARTED (PID $$)" >> "$LOGFILE"
echo "  IWO_PROJECT_ROOT=$IWO_PROJECT_ROOT" >> "$LOGFILE"

DIR="${IWO_PROJECT_ROOT:?IWO_PROJECT_ROOT not set — source .env or export it}/docs/agent-comms/.directives"
mkdir -p "$DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FILENAME=$(date +%s%N)-next-spec.json
FILEPATH="$DIR/$FILENAME"

# Write directive immediately (empty focus — ensures file always lands)
echo "{\"directive\":\"next-spec\",\"focus\":\"\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
echo "$(date -Iseconds) FILE WRITTEN: $FILEPATH" >> "$LOGFILE"

# Attempt zenity for optional focus enrichment
if FOCUS=$(zenity --entry --title="IWO: Plan Next Spec" --text="Optional focus area (e.g. calculators, shared infra):" --width=500 2>>"$LOGFILE") && [ -n "$FOCUS" ]; then
    echo "{\"directive\":\"next-spec\",\"focus\":\"$FOCUS\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
    echo "$(date -Iseconds) ENRICHED with focus: $FOCUS" >> "$LOGFILE"
fi

notify-send "IWO" "Next-spec directive queued — Planner will select and plan" 2>>"$LOGFILE" || true
echo "$(date -Iseconds) directive-next-spec.sh FINISHED" >> "$LOGFILE"
