# Installation & Configuration Guide

**Version:** 3.0.0 | **Updated:** 2026-03-17

This guide covers installing Ivan's Workflow Framework (IWF) and Ivan's Workflow Orchestrator (IWO) on a new machine. It assumes a Linux desktop environment (Ubuntu/Fedora/Arch) but the core system works anywhere Python 3.11+ and tmux run.

---

## System Requirements

**Required:**

- Python 3.11 or later
- tmux 3.0 or later
- Claude Code CLI (`claude` command), authenticated with your Anthropic account
- git

**Recommended:**

- GitHub CLI (`gh`) for PR creation from the Deployer agent
- jq for JSON processing in hook scripts
- Bitwarden CLI (`bw`) for automated credential management
- zenity and notify-send for desktop launcher dialogs and notifications

**Install on Ubuntu/Debian:**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tmux git gh jq zenity
```

**Install Claude Code CLI:**

Follow the official guide at https://docs.anthropic.com/claude-code. After installation, authenticate:

```bash
claude login
```

---

## Step 1: Clone the Repository

```bash
git clone https://github.com/No-Smoke/ivans-workflow.git ~/projects/ivans-workflow
cd ~/projects/ivans-workflow
```

You can clone to any path — nothing is hardcoded. The rest of this guide uses `~/projects/ivans-workflow` as the example.

## Step 2: Set Up the Python Environment

IWO requires a Python virtual environment with its dependencies:

```bash
cd ~/projects/ivans-workflow
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `iwo` and `iwo-tui` commands into the venv. Verify:

```bash
which iwo-tui
# Should print: ~/projects/ivans-workflow/.venv/bin/iwo-tui
```

You must activate the venv (`source .venv/bin/activate`) before running IWO in each new shell session, or invoke the commands with their full path.

---

## Step 3: Configure Environment (.env)

IWO reads all paths and service URLs from environment variables prefixed with `IWO_`. These are loaded from a `.env` file in the repo root.

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
IWO_PROJECT_ROOT=/absolute/path/to/your/project
```

This must be the absolute path to the project IWO will orchestrate — the repo that contains a `docs/agent-comms/` directory for handoff files.

### Minimal .env (orchestration only, no memory or notifications)

```bash
IWO_PROJECT_ROOT=/home/you/projects/my-app
```

This is sufficient to run IWO. Memory integration, notifications, and health checks are all disabled by default when their endpoints are not configured.

### Full .env (all features enabled)

```bash
# Required
IWO_PROJECT_ROOT=/home/you/projects/my-app

# Memory services
IWO_ENABLE_MEMORY=true
IWO_QDRANT_URL=http://localhost:6333
IWO_NEO4J_URI=bolt://localhost:7687
IWO_NEO4J_USER=neo4j
IWO_NEO4J_PASSWORD=your-password
IWO_OLLAMA_URL=http://localhost:11434
IWO_OLLAMA_MODEL=mxbai-embed-large

```bash
# Notifications
IWO_NTFY_TOPIC=my-project-alerts

# Health checks (post-deploy)
IWO_HEALTH_CHECK_URLS=https://myapp.com/api/health

# Deploy gates
IWO_AUTO_APPROVE_SAFE_DEPLOYS=true
IWO_AUTO_DEPLOY_ALL=false
IWO_AUTO_CONTINUE=false
```

The `.env` file is gitignored and never committed. All variables, their defaults, and descriptions are documented in `.env.example`.

---

## Step 4: Install the Framework into Your Project

IWF installs agent skills, hooks, commands, and rules into your project's `.claude/` directory:

```bash
cd ~/projects/ivans-workflow
./install.sh /path/to/your/project
```

The interactive installer asks about your tech stack (runtime, framework, language), agent count (4-6), and preferences. It takes about 2 minutes. The installer supports two modes:

**Symlink mode** (default, recommended): Core files are symlinked from the framework repo into your project. Updates to the framework (`git pull`) are picked up automatically.

**Copy mode**: Files are copied. You get a snapshot that won't change, but you must re-run the installer to get framework updates.

After installation, review the generated files:

```bash
cat /path/to/your/project/.claude/project-config.yaml   # Project configuration
cat /path/to/your/project/CLAUDE.md                       # Claude Code constitution
ls -la /path/to/your/project/.claude/skills/              # Agent skills
```

Customize your domain rules in `.claude/rules/project-domain.md` — this is where you add project-specific terminology, conventions, and constraints.

---

## Step 5: Launch the Agent Session

The tmux agent session must be running before IWO can orchestrate it:

```bash
~/projects/ivans-workflow/scripts/launch-tmux-agents.sh /path/to/your/project
```

This creates a tmux session with one window per agent (Planner, Builder, Reviewer, Tester, and optionally Deployer and Docs). Each agent starts Claude Code and initializes with its role-specific skill.

To attach to the session later:

```bash
tmux attach -t claude-agents
# Switch between agents: Ctrl+b 0 (Planner), Ctrl+b 1 (Builder), etc.
# Detach: Ctrl+b d
```

---

## Step 6: Start IWO

With the agent session running, launch the orchestrator:

```bash
cd ~/projects/ivans-workflow
source .venv/bin/activate
iwo-tui    # TUI dashboard (recommended)
```

Or headless (no dashboard, just orchestration):

```bash
iwo
```

IWO watches `docs/agent-comms/` in your project for handoff JSON files and automatically routes work between agents. The TUI shows agent status, pipeline progress, and a log panel.

---

## Step 7: Desktop Launchers (Optional)

On Linux with GNOME, install desktop launchers for convenient access:

```bash
~/projects/ivans-workflow/scripts/setup-new-machine.sh
```

This installs two launchers in your application menu:

- **Ivan's Workflow** — launches the tmux agent session. Right-click for: Kill Session, Plan Next Spec, View Git Log.
- **IWO — Orchestrator** — launches the IWO TUI. Right-click for: Run Headless, Plan Next Spec, Resolve Ops, Stop IWO.

Desktop launchers read paths from `.env` at install time, so run `setup-new-machine.sh` again if you change `IWO_PROJECT_ROOT`.

---

## Configuration Reference

### Environment Variables

All variables are optional except `IWO_PROJECT_ROOT`.

**Paths:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `IWO_PROJECT_ROOT` | current directory | Absolute path to the project being orchestrated |
| `IWO_LOG_DIR` | `{repo}/logs` | Directory for agent execution logs |
| `IWO_SKILLS_DIR` | `{repo}/skills` | Directory containing agent skills (bundled default, overridable) |
| `IWO_TMUX_SESSION` | `claude-agents` | Name of the tmux session |

**Memory Services (all optional — IWO works without them):**

| Variable | Default | Purpose |
|----------|---------|---------|
| `IWO_ENABLE_MEMORY` | `true` (but auto-disables if no endpoints) | Master switch for Qdrant + Neo4j |
| `IWO_QDRANT_URL` | (empty) | Qdrant REST endpoint |
| `IWO_QDRANT_API_KEY` | (empty) | Qdrant authentication key |
| `IWO_NEO4J_URI` | (empty) | Neo4j Bolt endpoint |
| `IWO_NEO4J_USER` | `neo4j` | Neo4j username |
| `IWO_NEO4J_PASSWORD` | (empty) | Neo4j password |
| `IWO_OLLAMA_URL` | `http://localhost:11434` | Ollama embedding endpoint |
| `IWO_OLLAMA_MODEL` | `mxbai-embed-large` | Embedding model for Qdrant vectors |

Memory auto-disables when neither `IWO_QDRANT_URL` nor `IWO_NEO4J_URI` is set. You do not need to set `IWO_ENABLE_MEMORY=false` explicitly.

**Notifications (all optional):**

| Variable | Default | Purpose |
|----------|---------|---------|
| `IWO_NTFY_TOPIC` | (empty) | ntfy.sh topic for mobile push. Empty = disabled |
| `IWO_NTFY_SERVER` | `https://ntfy.sh` | ntfy server URL |
| `IWO_WEBHOOK_URL` | (empty) | n8n or webhook URL for notifications |

To receive mobile push notifications, install the ntfy app (Android/iOS), subscribe to your topic, and set `IWO_NTFY_TOPIC` in `.env`.

**Deploy Gates:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `IWO_AUTO_APPROVE_SAFE_DEPLOYS` | `true` | Auto-approve deploys with no infrastructure changes |
| `IWO_AUTO_DEPLOY_ALL` | `false` | Bypass all deploy gates (for overnight autonomous runs) |
| `IWO_AUTO_CONTINUE` | `false` | Auto-queue next spec when a pipeline completes |

**Health Checks:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `IWO_HEALTH_CHECK_URLS` | (empty) | Comma-separated URLs to hit after deploy. Empty = skip |

### Configuration Precedence

1. Shell environment variables (highest priority)
2. `.env` file in the IWO repo root
3. Dataclass defaults in `iwo/config.py` (lowest priority)

Shell variables always win. This lets you override `.env` values for a single session:

```bash
IWO_AUTO_DEPLOY_ALL=true iwo-tui   # Enable overnight mode for this run only
```

---

## Multi-Machine Setup

The v3.0.0 env-driven configuration makes IWO portable across machines. There are several approaches depending on your setup:

### Git clone on each machine (recommended for most users)

Clone the repo on each machine, create a machine-specific `.env`, and install the venv:

```bash
git clone https://github.com/No-Smoke/ivans-workflow.git ~/projects/ivans-workflow
cd ~/projects/ivans-workflow
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
cp .env.example .env
# Edit .env for this machine's paths
```

Each machine has its own `.env` pointing to its own project directory and services.

### Nextcloud/Syncthing sync

If the repo is synced between machines via Nextcloud or Syncthing, the `.env` file syncs too. This works when both machines use identical paths (e.g., both have `~/Nextcloud/PROJECTS/...`). Caveats:

- The `.venv` directory contains absolute paths in its scripts. After sync, run `pip install -e .` on the new machine to fix them.
- Nextcloud can race on rapid file writes. Git is the source of truth — if Nextcloud corrupts files, `git checkout v3.0.0-pre-sync -- .` restores the last known-good state.

### Remote memory services via SSH tunnel

If memory services (Qdrant, Neo4j, Ollama) run on a different machine, use SSH tunnels to map them to localhost:

```bash
ssh -L 6333:qdrant-host:6333 -L 7687:neo4j-host:7687 -L 11434:ollama-host:11434 -N your-server
```

Then set localhost endpoints in `.env`:

```bash
IWO_QDRANT_URL=http://localhost:6333
IWO_NEO4J_URI=bolt://localhost:7687
IWO_OLLAMA_URL=http://localhost:11434
```

Or simply leave memory disabled — IWO orchestrates fine without it.

---

## Credential Management (Optional)

IWO includes a bundled credential-manager skill that integrates with Bitwarden CLI for secure credential handling in agent sessions.

### Initial setup

```bash
# Install Bitwarden CLI
sudo snap install bw   # or: npm install -g @bitwarden/cli

# Store your master password in GNOME Keyring (one-time)
secret-tool store --label='Bitwarden' bitwarden master
# Enter your Bitwarden master password when prompted
```

### Configure credentials for your project

In your project's `.claude/project-config.yaml`, define which Bitwarden entries to load:

```yaml
credentials:
  - alias: cloudflare
    bitwarden_entry: "MyProject-CloudFlare-API"
    env_vars:
      - name: CLOUDFLARE_API_TOKEN
        field: password
      - name: CLOUDFLARE_ACCOUNT_ID
        field: username
  - alias: github
    bitwarden_entry: "MyProject-GitHub-PAT"
    env_vars:
      - name: GITHUB_TOKEN
        field: password
```

Credentials are loaded automatically when agents launch via the SessionStart hook. The credential-manager uses a three-strategy fallback: BW_SESSION env var, GNOME Keyring auto-unlock, interactive prompt.

---

## Running as a Systemd Service (Optional)

IWO can run as a user-level systemd service for automatic startup:

```bash
# Copy the service file
cp ~/projects/ivans-workflow/iwo.service ~/.config/systemd/user/

# Edit the paths in the service file to match your installation
# The default uses Nextcloud paths — adjust to your setup

# Enable and start
systemctl --user enable iwo
systemctl --user start iwo
systemctl --user status iwo
```

The service loads `.env` via `EnvironmentFile` directives, so IWO picks up your configuration automatically.

Note: the tmux agent session must be running separately — the service only runs the IWO daemon, not the agents themselves.

---

## Verification Checklist

After installation, verify each component:

```bash
# 1. Python environment
source ~/projects/ivans-workflow/.venv/bin/activate
python3 -c "from iwo.config import IWOConfig; c = IWOConfig(); print(f'project_root={c.project_root}')"

# 2. Claude Code CLI
claude --version

# 3. tmux
tmux -V

# 4. Framework installed in project
ls /path/to/your/project/.claude/project-config.yaml

# 5. Agent session running
tmux has-session -t claude-agents && echo "Agents running" || echo "No agent session"

# 6. IWO starts
iwo-tui   # Should show TUI dashboard. Press 'q' to quit.
```

If memory services are configured:

```bash
# 7. Qdrant reachable
curl -s $IWO_QDRANT_URL/collections | head -c 100

# 8. Neo4j reachable
curl -s http://$(echo $IWO_NEO4J_URI | sed 's|bolt://||;s|:7687|:7474|') | head -c 100

# 9. Ollama reachable
curl -s $IWO_OLLAMA_URL/api/tags | head -c 100
```

---

## Troubleshooting Installation

**`iwo-tui: command not found`** — the venv is not activated. Run `source ~/projects/ivans-workflow/.venv/bin/activate`. Or use the full path: `~/projects/ivans-workflow/.venv/bin/iwo-tui`.

**`IWO_PROJECT_ROOT not set`** — the `.env` file is missing or doesn't contain `IWO_PROJECT_ROOT`. Run `cp .env.example .env` and edit it.

**`ModuleNotFoundError: No module named 'iwo'`** — the editable install is broken (common after syncing the venv between machines). Run `pip install -e .` from the repo root.

**Memory warnings in IWO log** — Qdrant, Neo4j, or Ollama is unreachable. Check the URLs in `.env`. Memory failures are non-fatal — IWO continues orchestrating.

**Desktop launchers don't work** — paths are baked in at install time. Re-run `scripts/setup-new-machine.sh` after changing `IWO_PROJECT_ROOT` in `.env`.

**tmux session name mismatch** — IWO defaults to `claude-agents`. If your launch script creates a differently-named session, set `IWO_TMUX_SESSION` in `.env` to match.

---

## Docker (Not Recommended)

IWO can technically run in a Docker container, but it requires bind-mounting the host's tmux socket, project directory, and `.env` file. The actual Claude Code agents still run on the host — the container only runs the orchestrator daemon. The venv + `.env` approach is simpler and has fewer failure modes for local development. Docker may be useful for running IWO as a headless service on a remote server, but that's an advanced configuration not covered here.

---

## What's Next

- [Getting Started](GETTING-STARTED.md) — first workflow walkthrough
- [Customization](CUSTOMIZATION.md) — add agents, rules, hooks, credentials
- [Architecture](ARCHITECTURE.md) — IWO internals, dispatch, memory, directives
- [Troubleshooting](TROUBLESHOOTING.md) — common runtime issues

---

*Ivan's Workflow v3.0.0 — https://github.com/No-Smoke/ivans-workflow*
