# Getting Started with Ivan's Workflow

## Prerequisites

Before installing Ivan's Workflow, ensure you have:

- **Claude Code CLI** — `claude` command available ([install guide](https://docs.anthropic.com/claude-code))
- **tmux** — Terminal multiplexer (`sudo apt install tmux`)
- **git** — Version control (`sudo apt install git`)
- **gh** — GitHub CLI for PR creation (`sudo apt install gh` or [install guide](https://cli.github.com))
- **jq** — JSON processor (`sudo apt install jq`)
- **Bitwarden CLI** (optional) — For credential management (`sudo snap install bw`)

## Quick Install

```bash
# Clone the framework
git clone https://github.com/No-Smoke/ivans-workflow.git ~/projects/ivans-workflow

# Navigate to your project
cd /path/to/your/project

# Run the installer
~/projects/ivans-workflow/install.sh
```

The installer will ask you about your project's tech stack, agent count, and preferences, then set up everything in your `.claude/` directory.

## First Project Setup

### 1. Run the Installer

```bash
~/projects/ivans-workflow/install.sh
```

Answer the prompts:
- **Project name:** Your project's display name
- **Runtime:** cloudflare-workers / node / deno / python
- **Framework:** hono / express / nextjs / fastapi / none
- **Language:** typescript / javascript / python
- **Schema-first?** If your project uses a JSON schema → TypeScript generation pipeline
- **Spec-driven?** If you maintain specification documents
- **Agent count:** 4 (standard), 5 (+Deployer), or 6 (+Deployer +Docs)
- **Link mode:** symlink (recommended) or copy

### 2. Review Generated Files

After installation, check:

```bash
cat .claude/project-config.yaml    # Your project configuration
cat CLAUDE.md                       # Project constitution for Claude Code
ls -la .claude/agents/              # Subagent definitions (symlinks)
ls -la .claude/commands/            # Slash commands (symlinks)
ls -la .claude/hooks/               # Quality gate hooks (symlinks)
cat .claude/hooks.json              # Hook configuration
```

### 3. Customize Domain Rules

Edit `.claude/rules/project-domain.md` with your project-specific rules — domain terminology, business logic constraints, coding conventions that go beyond the stack defaults.

### 4. Set Up Credentials (Optional)

If you use Bitwarden:

```bash
# Store master password for auto-unlock (one-time)
secret-tool store --label='Bitwarden' bitwarden master

# Or manually before each session:
export BW_SESSION=$(bw unlock --raw)
```

### 5. Launch Agents

```bash
# Start the multi-agent tmux session
.claude/scripts/launch.sh

# Or if using the global script:
~/projects/ivans-workflow/scripts/launch-tmux-agents.sh /path/to/your/project
```

### 6. Basic Workflow

Once agents are running:

1. **Switch to Planner** (Ctrl+b 0) — Give it a task or spec ID
2. **Planner creates implementation plan** → Handoff to Builder
3. **Switch to Builder** (Ctrl+b 1) — Builder implements the plan
4. **Builder completes** → Handoff to Reviewer
5. **Switch to Reviewer** (Ctrl+b 2) — Reviews code quality
6. **If issues found** → Back to Builder. If approved → Tester
7. **Switch to Tester** (Ctrl+b 3) — Runs full test suite
8. **Tests pass** → Done (or → Deployer if using 5+ agents)

### Useful tmux Commands

| Key | Action |
|-----|--------|
| Ctrl+b d | Detach from session (agents keep running) |
| Ctrl+b 0-5 | Switch to agent window |
| Ctrl+b [ | Scroll mode (q to exit) |
| Ctrl+b c | Create new window |

### Useful Slash Commands

| Command | What It Does |
|---------|-------------|
| /test-and-commit | Run quality checks, commit if all pass |
| /commit-push-pr | Commit, push branch, create PR |
| /ralph-loop | Auto-fix loop (max 50 iterations) |
| /workflow-start TASK-ID | Initialize workflow for a task |
| /workflow-next | Advance to next agent |
| /workflow-status | Show task progress |

## Next Steps

- [Customization Guide](CUSTOMIZATION.md) — Add custom agents, rules, and hooks
- [Agent Reference](AGENT-REFERENCE.md) — Complete reference for all agents
- [Troubleshooting](TROUBLESHOOTING.md) — Common issues and solutions
