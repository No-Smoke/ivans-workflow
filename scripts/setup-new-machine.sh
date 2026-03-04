#!/bin/bash
# IWO + IWF Setup Script for new machines
# Run this on any machine with Nextcloud synced to set up the full system.
#
# Prerequisites:
#   - Nextcloud synced with ~/Nextcloud/PROJECTS/
#   - Python 3.11+, tmux, zenity, notify-send
#   - Claude Code CLI authenticated (claude login)

set -e

IWO_DIR="$HOME/Nextcloud/PROJECTS/ivans-workflow-orchestrator"
EBATT_DIR="$HOME/Nextcloud/PROJECTS/ebatt-ai/ebatt"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "=== IWO + IWF Setup ==="
echo ""

# --- Step 1: Verify paths exist ---
echo "[1/6] Checking Nextcloud paths..."
if [ ! -d "$IWO_DIR" ]; then
    echo "ERROR: $IWO_DIR not found. Is Nextcloud synced?"
    exit 1
fi
if [ ! -d "$EBATT_DIR" ]; then
    echo "ERROR: $EBATT_DIR not found. Is Nextcloud synced?"
    exit 1
fi
echo "  OK"

# --- Step 2: Python venv ---
echo "[2/6] Setting up Python venv..."
cd "$IWO_DIR"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "  Created .venv"
else
    echo "  .venv exists"
fi
source .venv/bin/activate
pip install -q -r requirements.txt
pip install -q -e .
echo "  Dependencies installed"

# --- Step 3: Verify entry points ---
echo "[3/6] Verifying iwo/iwo-tui commands..."
if command -v iwo-tui &>/dev/null; then
    echo "  iwo-tui: OK"
else
    echo "  WARNING: iwo-tui not on PATH. Run: source $IWO_DIR/.venv/bin/activate"
fi

# --- Step 4: Desktop launchers ---
echo "[4/6] Installing desktop launchers..."
mkdir -p "$DESKTOP_DIR"


# IWF launcher
cat > "$DESKTOP_DIR/boris-workflow.desktop" << 'LAUNCHER'
[Desktop Entry]
Version=1.0
Type=Application
Name=Ivan's Workflow
Comment=Launch 7-agent headless workflow for ebatt-ai (v5.6)
Icon=applications-development
Exec=x-terminal-emulator -e bash -c 'cd /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt && ./scripts/boris-workflow/launch-tmux-agents-v5.sh; exec bash'
Terminal=false
Categories=Development;IDE;
Keywords=claude;ai;development;workflow;tmux;
StartupNotify=true
Actions=KillSession;NextSpec;StartSpec;Resume;ViewLogs;LoadCredentials;

[Desktop Action KillSession]
Name=⬛ Kill Session
Exec=bash -c 'tmux kill-session -t claude-agents 2>/dev/null && notify-send "Ivan'\''s Workflow" "Session terminated" || notify-send "Ivan'\''s Workflow" "No session to kill"'

[Desktop Action NextSpec]
Name=🧠 Plan Next Spec
Exec=/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/scripts/directive-next-spec.sh

[Desktop Action StartSpec]
Name=▶ Start Spec...
Exec=bash -c 'SPEC=$(zenity --entry --title="Start Spec" --text="Spec ID (e.g. EBATT-011):" --width=400 2>/dev/null); if [ -n "$SPEC" ]; then CONTEXT=$(zenity --entry --title="Additional Context" --text="Optional instructions for Planner:" --width=500 2>/dev/null); DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"start-spec\",\"specId\":\"$SPEC\",\"context\":\"$CONTEXT\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-start-spec.json"; notify-send "Ivan'\''s Workflow" "Start directive queued for $SPEC"; fi'

[Desktop Action Resume]
Name=▶ Resume Spec...
Exec=bash -c 'SPEC=$(zenity --entry --title="Resume Spec" --text="Spec ID to resume:" --width=400 2>/dev/null); if [ -n "$SPEC" ]; then DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"resume\",\"specId\":\"$SPEC\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-resume.json"; notify-send "Ivan'\''s Workflow" "Resume directive queued for $SPEC"; fi'

[Desktop Action ViewLogs]
Name=📋 View Git Log
Exec=x-terminal-emulator -e bash -c 'cd /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt && git log --oneline -30; exec bash'

[Desktop Action LoadCredentials]
Name=🔑 Load Credentials Only
Exec=x-terminal-emulator -e bash -c 'source /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/.claude/scripts/load-credentials.sh; echo ""; echo "Press Enter to close."; read'
LAUNCHER


# IWO launcher
cat > "$DESKTOP_DIR/iwo.desktop" << 'LAUNCHER'
[Desktop Entry]
Version=1.0
Type=Application
Name=IWO — Orchestrator
Comment=Ivan's Workflow Orchestrator — headless dispatch via claude -p (v2.9.1)
Icon=/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/assets/iwo-icon-256.png
Exec=x-terminal-emulator -e bash -c 'cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator && source .venv/bin/activate && python -m iwo.tui; exec bash'
Terminal=false
Categories=Development;IDE;
Keywords=iwo;orchestrator;workflow;tmux;agents;automation;
StartupNotify=true
Actions=Headless;NextSpec;StartSpec;Resume;Reconcile;Status;Pause;Unpause;CancelSpec;Stop;Logs;

[Desktop Action Headless]
Name=Run Headless (no TUI)
Exec=x-terminal-emulator -e bash -c 'cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator && source .venv/bin/activate && python -m iwo.daemon; exec bash'

[Desktop Action NextSpec]
Name=🧠 Plan Next Spec
Exec=/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/scripts/directive-next-spec.sh

[Desktop Action StartSpec]
Name=▶ Start Spec...
Exec=bash -c 'SPEC=$(zenity --entry --title="IWO: Start Spec" --text="Spec ID (e.g. EBATT-011):" --width=400 2>/dev/null); if [ -n "$SPEC" ]; then CONTEXT=$(zenity --entry --title="IWO: Additional Context" --text="Optional instructions for Planner:" --width=500 2>/dev/null); DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"start-spec\",\"specId\":\"$SPEC\",\"context\":\"$CONTEXT\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-start-spec.json"; notify-send "IWO" "Start directive queued for $SPEC"; fi'

[Desktop Action Resume]
Name=▶ Resume Spec...
Exec=bash -c 'SPEC=$(zenity --entry --title="IWO: Resume Spec" --text="Spec ID to resume:" --width=400 2>/dev/null); if [ -n "$SPEC" ]; then DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"resume\",\"specId\":\"$SPEC\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-resume.json"; notify-send "IWO" "Resume directive queued for $SPEC"; fi'

[Desktop Action Reconcile]
Name=🔄 Reconcile Pipeline
Exec=bash -c 'DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"reconcile\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-reconcile.json"; notify-send "IWO" "Reconcile directive queued"'

[Desktop Action Status]
Name=📊 Status Report
Exec=bash -c 'DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"status\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-status.json"; notify-send "IWO" "Status report requested"'

[Desktop Action Pause]
Name=⏸ Pause Dispatch
Exec=bash -c 'DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"pause\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-pause.json"; notify-send "IWO" "Pause directive queued"'

[Desktop Action Unpause]
Name=▶ Unpause Dispatch
Exec=bash -c 'DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"unpause\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-unpause.json"; notify-send "IWO" "Unpause directive queued"'

[Desktop Action CancelSpec]
Name=🛑 Cancel Spec...
Exec=bash -c 'SPEC=$(zenity --entry --title="IWO: Cancel Spec" --text="Spec ID to cancel:" --width=400 2>/dev/null); if [ -n "$SPEC" ]; then DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"; mkdir -p "$DIR"; echo "{\"directive\":\"cancel-spec\",\"specId\":\"$SPEC\",\"timestamp\":\"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\"}" > "$DIR/$(date +%%s%%N)-cancel.json"; notify-send "IWO" "Cancel directive queued for $SPEC"; fi'

[Desktop Action Stop]
Name=⬛ Stop IWO
Exec=bash -c 'pkill -f "python -m iwo" 2>/dev/null && notify-send "IWO" "Orchestrator stopped" || notify-send "IWO" "Not running"'

[Desktop Action Logs]
Name=📋 View Logs
Exec=x-terminal-emulator -e bash -c 'less +F /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/logs/iwo.log 2>/dev/null || echo "No log file found."; exec bash'
LAUNCHER

echo "  boris-workflow.desktop installed"
echo "  iwo.desktop installed"

# --- Step 5: Refresh desktop database ---
echo "[5/6] Refreshing desktop database..."
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
echo "  OK"

# --- Step 6: Verify ---
echo "[6/6] Verification..."
echo "  IWO dir:     $IWO_DIR"
echo "  eBatt dir:   $EBATT_DIR"
echo "  Python:      $(python3 --version)"
echo "  tmux:        $(tmux -V 2>/dev/null || echo 'NOT INSTALLED')"
echo "  zenity:      $(zenity --version 2>/dev/null || echo 'NOT INSTALLED')"
echo "  claude:      $(claude --version 2>/dev/null || echo 'NOT INSTALLED')"
echo ""
echo "=== Setup complete ==="
echo ""
echo "To start working:"
echo "  1. Click 'Ivan's Workflow' in your app menu (or run: $EBATT_DIR/scripts/boris-workflow/launch-tmux-agents-v5.sh)"
echo "  2. Click 'IWO — Orchestrator' in your app menu"
echo "  3. Open http://localhost:8787 for the Kanban dashboard"
echo "  4. Right-click either launcher for actions (Plan Next Spec, Start Spec, etc.)"
