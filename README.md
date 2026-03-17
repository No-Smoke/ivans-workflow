# Ivan's Workflow

**Multi-agent Claude Code development framework with quality gates, structured handoffs, and portable project configuration.**

> Run 4–6 specialized AI agents in tmux — Planner, Builder, Reviewer, Tester, and optional Deployer/Docs — with automated quality checks, risk classification, and structured inter-agent communication.

---

## Quick Install

```bash
git clone https://github.com/No-Smoke/ivans-workflow.git ~/projects/ivans-workflow
cd /path/to/your/project
~/projects/ivans-workflow/install.sh
```

The interactive installer configures everything based on your tech stack. Takes about 2 minutes.

---

## What It Does

Ivan's Workflow transforms a single Claude Code session into a coordinated multi-agent development team. Each agent has a defined role, explicit boundaries, and structured handoffs — preventing the "eager execution" problem where a single AI session drifts outside its lane.

```
  Task / Spec
      │
      ▼
  🔵 Planner ───► Architecture & implementation plan
      │
      ▼
  🟢 Builder ───► Code + tests + incremental commits
      │
      ▼
  🟡 Reviewer ──► Quality review (automated + manual)
      │
      ▼
  🟣 Tester ────► Full test suite + integration verification
      │
      ▼
  🔴 Deployer ─► PR creation + deployment (optional)
      │
      ▼
  🟠 Docs ──────► Documentation updates (optional)
```

Each agent runs in its own tmux window. Switch between them with `Ctrl+b 0-5`.

---

## Architecture

```
ivans-workflow/                    Your Project
├── core/                          ├── .claude/
│   ├── agents/     ──symlink──►   │   ├── agents/
│   ├── commands/   ──symlink──►   │   ├── commands/
│   ├── hooks/      ──symlink──►   │   ├── hooks/
│   ├── skills/     ──symlink──►   │   ├── skills/
│   ├── rules/      ──symlink──►   │   ├── rules/
│   └── scripts/    ──symlink──►   │   └── scripts/
├── templates/                     │
│   └── (generates) ──────────►    ├── project-config.yaml
│                                  ├── hooks.json
│                                  ├── settings.json
├── scripts/                       ├── CLAUDE.md
│   └── launch-tmux-agents.sh      └── rules/
└── install.sh                         ├── project-stack.md
                                       └── project-domain.md
```

The framework core stays in its own repo. Your project gets symlinks to the shared components plus generated config files specific to your stack. Update the framework with `git pull` — symlinks pick up changes automatically.

---

## Agents

### Main Agents (tmux Windows)

| # | Agent | Mode | Purpose |
|---|-------|------|---------|
| 0 | 🔵 Planner | Plan | Architecture, design, task breakdown |
| 1 | 🟢 Builder | Auto-accept | Implementation execution |
| 2 | 🟡 Reviewer | Interactive | Code review, quality checks |
| 3 | 🟣 Tester | Interactive | Test execution, verification |
| 4 | 🔴 Deployer | Interactive | Deploy + PR creation (optional) |
| 5 | 🟠 Docs | Interactive | Documentation (optional) |

### Subagents (Invoked by Main Agents)

| Agent | Model | Purpose |
|-------|-------|---------|
| @code-simplifier | inherit | Reduce complexity without changing behavior |
| @verify-app | haiku | Quick compile + health check verification |
| @ux-reviewer | inherit | UI/UX usability assessment |
| @schema-guardian | sonnet | Safe schema changes with backup/rollback |
| @integration-tester | sonnet | Integration/E2E tests against running server |
| @pr-architect | haiku | Structured PRs with risk classification |
| @perf-monitor | haiku | Bundle size, cold start, latency checks |
| @docs-agent | sonnet | Documentation updates |

---

## Hooks & Quality Gates

| Hook | Type | What It Does |
|------|------|-------------|
| builder-guard.sh | PreToolUse | Blocks writes to generated files, schema (use @schema-guardian), Node builtins in Workers |
| verify-work.sh | Stop (blocking) | Runs typecheck → lint → tests before agent can complete |
| pre-review-checks.sh | Script | Automated pre-review: imports, hardcoded values, error handling |
| classify-risk.sh | Script | Classifies git diff as CRITICAL/HIGH/MEDIUM/LOW |
| metrics-logger.mjs | PostToolUse | Logs tool usage, duration, token estimates |
| prompt-handoff.sh | Stop | Injects handoff reminder when task is in progress |

---

## Slash Commands

| Command | What It Does |
|---------|-------------|
| /test-and-commit | Run typecheck + lint + test → commit if all pass |
| /commit-push-pr | Commit, push branch, create PR via @pr-architect |
| /ralph-loop | Auto-retry quality checks + fix loop (max 50 iterations) |
| /workflow-start ID | Initialize workflow for a task/spec |
| /workflow-next | Advance to next agent in sequence |
| /workflow-status | Show all active tasks and their stage |
| /agent-handoff | Generate structured handoff for next agent |
| /compact-and-continue | Save state, compact context, restore state |
| /chat-completion | End-of-session wrap-up with handoff summary |

---

## Configuration

Everything project-specific lives in `.claude/project-config.yaml`:

```yaml
project:
  name: "My Project"

stack:
  runtime: cloudflare-workers    # node | deno | python
  framework: hono                # express | nextjs | fastapi | none
  language: typescript
  test_runner: vitest
  deploy_command: "npx wrangler deploy"
  dev_command: "npx wrangler dev"

schema:
  enabled: true
  definitions: "schema/definitions.json"
  generated_types: "generated/types.ts"

agents:
  count: 5
  builder_mode: auto-accept

quality:
  test_command: "npm test"
  lint_command: "npm run lint"
  coverage_threshold: 80

credentials:
  - alias: cloudflare
    bitwarden_entry: "MyProject-CloudFlare-API"
    env_vars:
      - name: CLOUDFLARE_API_TOKEN
        field: password
```

All agents, hooks, and commands read from this config — nothing is hardcoded.

---

## Credential Management

Ivan's Workflow integrates with Bitwarden CLI for secure credential management using a three-strategy fallback:

1. **BW_SESSION env var** — Fastest, most portable
2. **GNOME Keyring auto-unlock** — Convenient for desktop sessions
3. **Interactive prompt** — Fallback when automation fails

Credentials are loaded automatically when agents launch. See [Credential Manager docs](docs/CUSTOMIZATION.md#configuring-credentials) for setup.

---

## Comparison with Vanilla Claude Code

| Feature | Vanilla Claude Code | Ivan's Workflow |
|---------|-------------------|-----------------|
| Agent roles | Single session, no boundaries | 4-6 agents with explicit role limits |
| Quality gates | Manual | Automated hooks (typecheck, lint, test) |
| Code review | Self-review | Separate Reviewer agent with pre-checks |
| Handoffs | Copy/paste context | Structured JSON handoff files |
| Risk classification | None | Automatic CRITICAL/HIGH/MEDIUM/LOW |
| Schema protection | None | @schema-guardian with backup/rollback |
| Credential management | Manual env vars | Bitwarden integration with auto-unlock |
| Multi-project | Single session | Named tmux sessions per project |
| Slash commands | Default set | 9 workflow commands (/ralph-loop, etc.) |

---

## Supported Stacks

| Runtime | Frameworks | Languages |
|---------|-----------|-----------|
| CloudFlare Workers | Hono | TypeScript |
| Node.js | Express, Next.js | TypeScript, JavaScript |
| Deno | Fresh, Hono | TypeScript |
| Python | FastAPI | Python |

The installer generates stack-specific rules (e.g., "no Node builtins in Workers code") and configures hooks accordingly.

---

## Documentation

- [Getting Started](docs/GETTING-STARTED.md) — Prerequisites, installation, first workflow
- [Architecture (IWO)](docs/ARCHITECTURE.md) — Orchestrator internals, config, dispatch, memory
- [Customization](docs/CUSTOMIZATION.md) — Add agents, rules, hooks, credentials
- [Agent Reference](docs/AGENT-REFERENCE.md) — Complete reference for all agents
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues and solutions
- [Changelog](CHANGELOG.md) — Release history and fixes

---

## Ivan's Workflow Orchestrator (IWO)

IWO is the automation layer that sits above the framework. While IWF defines agent roles, skills, and handoff protocols, IWO automates the handoff routing — monitoring for handoff JSON files, dispatching work via headless `claude -p` invocations, and managing pipeline state.

### IWO Quick Start

```bash
# Already have the repo cloned from the IWF install above
cd ~/projects/ivans-workflow

# Set up Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure for your project
cp .env.example .env
# Edit .env — set IWO_PROJECT_ROOT to your project path

# Launch the TUI
iwo-tui
```

IWO requires a tmux session with agents already running (launched by `scripts/launch-tmux-agents.sh`). It watches `docs/agent-comms/` for handoff files and dispatches work automatically.

### Key Features

**Headless dispatch** — agents receive work via `claude -p` processes, not tmux send-keys injection. Deterministic idle detection via `pane_current_command`.

**Directive processor** — external control via JSON files dropped into `.directives/`. Desktop launchers, cron jobs, and CLI scripts can queue directives like `start-spec`, `next-spec`, `pause`, `resolve-ops`.

**Ops actions register** — tracks manual infrastructure tasks (migrations, secrets, DNS) auto-extracted from handoff JSON. Priority-based ntfy notifications.

**Memory integration** (optional) — stores pipeline telemetry to Qdrant (semantic search) and Neo4j (graph queries). Degrades gracefully when unavailable.

**Environment-driven config** — all paths and service URLs via `IWO_*` environment variables loaded from `.env`. No hardcoded paths in source. See [Architecture docs](docs/ARCHITECTURE.md) for the full variable reference.

### IWO TUI

```
iwo-tui    # Interactive dashboard
iwo        # Headless daemon (no TUI)
```

TUI keybindings: `q` quit, `d` approve deploy, `r` refresh, `p` pause, `a` toggle auto-deploy, `D` toggle auto-continue.

For full IWO documentation see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Contributing

Contributions welcome. The framework is designed to be generic — project-specific features belong in project overlays, not in the core.

1. Fork the repo
2. Create a feature branch
3. Make your changes
4. Submit a PR

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Ivan's Workflow — https://github.com/No-Smoke/ivans-workflow*
