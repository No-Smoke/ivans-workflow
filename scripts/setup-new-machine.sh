#!/bin/bash
# IWO + IWF Setup Script for new machines
# Reads paths from .env (or prompts interactively if not found).
#
# Prerequisites:
#   - Python 3.11+, tmux, zenity, notify-send
#   - Claude Code CLI authenticated (claude login)
#   - IWO repo cloned (this script lives in it)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IWO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_DIR="$HOME/.local/share/applications"

# Load .env for IWO_PROJECT_ROOT
if [ -f "$IWO_DIR/.env" ]; then
    set -a; source "$IWO_DIR/.env"; set +a
fi

# Resolve project root — prompt if not set
if [ -z "$IWO_PROJECT_ROOT" ]; then
    echo "IWO_PROJECT_ROOT not set in .env"
    read -rp "Path to target project (e.g. /home/you/projects/my-app): " IWO_PROJECT_ROOT
    if [ -z "$IWO_PROJECT_ROOT" ]; then
        echo "ERROR: IWO_PROJECT_ROOT is required."
        exit 1
    fi
fi
EBATT_DIR="$IWO_PROJECT_ROOT"

echo "=== IWO + IWF Setup ==="
echo ""

# --- Step 1: Verify paths exist ---
echo "[1/6] Checking paths..."
if [ ! -d "$IWO_DIR" ]; then
    echo "ERROR: IWO directory not found at $IWO_DIR"
    exit 1
fi
if [ ! -d "$EBATT_DIR" ]; then
    echo "ERROR: Project directory not found at $EBATT_DIR"
    echo "       Set IWO_PROJECT_ROOT in $IWO_DIR/.env"
    exit 1
fi
echo "  IWO:     $IWO_DIR"
echo "  Project: $EBATT_DIR"

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

# Helper: all desktop actions use a wrapper that sources .env for IWO_PROJECT_ROOT
DIRECTIVE_HELPER="bash -c 'source $IWO_DIR/.env 2>/dev/null; DIR=\$IWO_PROJECT_ROOT/docs/agent-comms/.directives; mkdir -p \$DIR"

# IWF launcher
cat > "$DESKTOP_DIR/boris-workflow.desktop" <<LAUNCHER
[Desktop Entry]
Version=1.0
Type=Application
Name=Ivan's Workflow
Comment=Launch agent workflow
Icon=applications-development
Exec=x-terminal-emulator -e bash -c 'cd $EBATT_DIR && ./scripts/boris-workflow/launch-tmux-agents-v5.sh; exec bash'
Terminal=false
Categories=Development;IDE;
Keywords=claude;ai;development;workflow;tmux;
StartupNotify=true
Actions=KillSession;NextSpec;ViewLogs;

[Desktop Action KillSession]
Name=Kill Session
Exec=bash -c 'tmux kill-session -t claude-agents 2>/dev/null && notify-send "IWF" "Session terminated" || notify-send "IWF" "No session"'

[Desktop Action NextSpec]
Name=Plan Next Spec
Exec=$IWO_DIR/scripts/directive-next-spec.sh

[Desktop Action ViewLogs]
Name=View Git Log
Exec=x-terminal-emulator -e bash -c 'cd $EBATT_DIR && git log --oneline -30; exec bash'
LAUNCHER

# IWO launcher
cat > "$DESKTOP_DIR/iwo.desktop" <<LAUNCHER
[Desktop Entry]
Version=1.0
Type=Application
Name=IWO — Orchestrator
Comment=Ivan's Workflow Orchestrator
Icon=$IWO_DIR/assets/iwo-icon-256.png
Exec=x-terminal-emulator -e bash -c 'cd $IWO_DIR && source .venv/bin/activate && python -m iwo.tui; exec bash'
Terminal=false
Categories=Development;IDE;
Keywords=iwo;orchestrator;workflow;tmux;
StartupNotify=true
Actions=Headless;NextSpec;ResolveOps;Stop;

[Desktop Action Headless]
Name=Run Headless (no TUI)
Exec=x-terminal-emulator -e bash -c 'cd $IWO_DIR && source .venv/bin/activate && python -m iwo.daemon; exec bash'

[Desktop Action NextSpec]
Name=Plan Next Spec
Exec=$IWO_DIR/scripts/directive-next-spec.sh

[Desktop Action ResolveOps]
Name=Resolve Ops Actions
Exec=$IWO_DIR/scripts/directive-resolve-ops.sh

[Desktop Action Stop]
Name=Stop IWO
Exec=bash -c 'pkill -f "python -m iwo" 2>/dev/null && notify-send "IWO" "Stopped" || notify-send "IWO" "Not running"'
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
