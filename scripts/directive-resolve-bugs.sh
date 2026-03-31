#!/bin/bash
# IWO Directive: Resolve Bugs (GitHub Issues)
# Called from desktop launcher right-click menu or CLI
# Drops a resolve-bugs directive into IWO's directive directory

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if IWO_PROJECT_ROOT not already set
if [ -z "$IWO_PROJECT_ROOT" ] && [ -f "$IWO_ROOT/.env" ]; then
    set -a; source "$IWO_ROOT/.env"; set +a
fi

LOGFILE="/tmp/iwo-directive-debug.log"
echo "$(date -Iseconds) directive-resolve-bugs.sh STARTED (PID $$)" >> "$LOGFILE"

DIR="${IWO_PROJECT_ROOT:?IWO_PROJECT_ROOT not set — source .env or export it}/docs/agent-comms/.directives"
mkdir -p "$DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FILENAME=$(date +%s%N)-resolve-bugs.json
FILEPATH="$DIR/$FILENAME"

# Default filter and max
FILTER="${1:-all}"
MAX="${2:-10}"

# Write directive immediately with defaults
echo "{\"directive\":\"resolve-bugs\",\"filter\":\"$FILTER\",\"max_bugs\":$MAX,\"context\":\"Desktop launcher trigger\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
echo "$(date -Iseconds) FILE WRITTEN: $FILEPATH" >> "$LOGFILE"

# Attempt zenity for optional filter selection
if FILTER_CHOICE=$(zenity --list --title="IWO: Resolve Bugs" \
    --text="Select which bugs to resolve:" \
    --column="Filter" --column="Description" \
    "all" "All approved bugs (critical/high gated, medium/low auto)" \
    "critical" "Critical priority only" \
    "high" "Critical + high priority" \
    --width=550 --height=250 2>>"$LOGFILE"); then
    if [ -n "$FILTER_CHOICE" ]; then
        echo "{\"directive\":\"resolve-bugs\",\"filter\":\"$FILTER_CHOICE\",\"max_bugs\":$MAX,\"context\":\"Desktop launcher trigger\",\"timestamp\":\"$TIMESTAMP\"}" > "$FILEPATH"
        echo "$(date -Iseconds) ENRICHED with filter: $FILTER_CHOICE" >> "$LOGFILE"
    fi
fi

notify-send "IWO" "Resolve-bugs directive queued (filter: ${FILTER_CHOICE:-$FILTER}) — pipeline will dispatch" 2>>"$LOGFILE" || true
echo "$(date -Iseconds) directive-resolve-bugs.sh FINISHED" >> "$LOGFILE"
