#!/bin/bash
TIMESTAMP=$(date +%s)
DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"
mkdir -p "$DIR"
cat > "$DIR/resolve-ops-test-$TIMESTAMP.json" << EOF
{
    "directive": "resolve-ops",
    "filter": "all",
    "context": "Manual test from test-ops-agent.sh"
}
EOF
echo "Dropped resolve-ops directive: $DIR/resolve-ops-test-$TIMESTAMP.json"
