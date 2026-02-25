#!/bin/bash
# IWO Directive: Plan Next Spec
# Called from desktop launcher right-click menu
DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"
mkdir -p "$DIR"

FOCUS=$(zenity --entry --title="IWO: Plan Next Spec" --text="Optional focus area (e.g. calculators, shared infra):" --width=500 2>/dev/null) || true

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FILENAME=$(date +%s%N)-next-spec.json

echo "{\"directive\":\"next-spec\",\"focus\":\"$FOCUS\",\"timestamp\":\"$TIMESTAMP\"}" > "$DIR/$FILENAME"

notify-send "IWO" "Next-spec directive queued — Planner will select and plan"
