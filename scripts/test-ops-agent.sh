#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -z "$IWO_PROJECT_ROOT" ] && [ -f "$IWO_ROOT/.env" ]; then
    set -a; source "$IWO_ROOT/.env"; set +a
fi

TIMESTAMP=$(date +%s)
DIR="${IWO_PROJECT_ROOT:?IWO_PROJECT_ROOT not set}/docs/agent-comms/.directives"
mkdir -p "$DIR"
cat > "$DIR/resolve-ops-test-$TIMESTAMP.json" << EOF
{
    "directive": "resolve-ops",
    "filter": "all",
    "context": "Manual test from test-ops-agent.sh"
}
EOF
echo "Dropped resolve-ops directive: $DIR/resolve-ops-test-$TIMESTAMP.json"
