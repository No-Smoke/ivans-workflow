#!/bin/bash
# IWO Directive: Resolve Ops Actions
# Called from desktop launcher right-click menu
# v2: env-driven paths, .env auto-load

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if IWO_PROJECT_ROOT not already set
if [ -z "$IWO_PROJECT_ROOT" ] && [ -f "$IWO_ROOT/.env" ]; then
    set -a; source "$IWO_ROOT/.env"; set +a
fi

LOGFILE="/tmp/iwo-directive-debug.log"
echo "$(date -Iseconds) directive-resolve-ops.sh STARTED (PID $$)" >> "$LOGFILE"

DIR="${IWO_PROJECT_ROOT:?IWO_PROJECT_ROOT not set — source .env or export it}/docs/agent-comms/.directives"
mkdir -p "$DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FILENAME=$(date +%s%N)-resolve-ops.json
FILEPATH="$DIR/$FILENAME"

# Write directive immediately with default filter (all)
echo "{\"directive\":\"resolve-ops\",\"filter\":\"all\",\"context\":\"Desktop launcher trigger\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
echo "$(date -Iseconds) FILE WRITTEN: $FILEPATH" >> "$LOGFILE"

# Attempt zenity for optional filter selection
if FILTER=$(zenity --list --title="IWO: Resolve Ops Actions" \
    --text="Select which ops actions to resolve:" \
    --column="Filter" --column="Description" \
    "all" "All pending actions (gated categories need 'o' approval)" \
    "critical" "Critical priority only" \
    "auto-only" "Auto-approvable only (migration, config — no gate)" \
    --width=550 --height=280 2>>"$LOGFILE"); then
    if [ -n "$FILTER" ]; then
        echo "{\"directive\":\"resolve-ops\",\"filter\":\"$FILTER\",\"context\":\"Desktop launcher trigger\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
        echo "$(date -Iseconds) ENRICHED with filter: $FILTER" >> "$LOGFILE"
    fi
fi

notify-send "IWO" "Resolve-ops directive queued (filter: ${FILTER:-all}) — Agent 007 will dispatch" 2>>"$LOGFILE" || true
echo "$(date -Iseconds) directive-resolve-ops.sh FINISHED" >> "$LOGFILE"
