#!/bin/bash
# IWO Directive: Plan Next Spec
# Called from desktop launcher right-click menu
# v2: write-first design + debug logging

LOGFILE="/tmp/iwo-directive-debug.log"
echo "$(date -Iseconds) directive-next-spec.sh STARTED (PID $$)" >> "$LOGFILE"
echo "  DISPLAY=$DISPLAY WAYLAND_DISPLAY=$WAYLAND_DISPLAY XDG_SESSION_TYPE=$XDG_SESSION_TYPE" >> "$LOGFILE"

DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"
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
