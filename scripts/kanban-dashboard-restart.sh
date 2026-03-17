#!/bin/bash
# IWO Kanban Dashboard — restart
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/kanban-dashboard-stop.sh"
sleep 1
"$SCRIPT_DIR/kanban-dashboard-start.sh"
