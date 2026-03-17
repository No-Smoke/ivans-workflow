# Ivan's Workflow Orchestrator (IWO) — Architecture Guide

**Version:** 3.0.0 | **Updated:** 2026-03-17
**Repository:** [No-Smoke/ivans-workflow](https://github.com/No-Smoke/ivans-workflow)

## Overview

Ivan's Workflow Orchestrator (IWO) is a Python daemon that automates handoffs between multiple Claude Code AI agents running in tmux sessions. It monitors for handoff JSON files, validates them, and dispatches work via headless `claude -p` invocations. No interactive prompt detection, no send-keys injection.

IWO is designed for Ivan's Workflow (IWF) — a six-agent development pipeline where each agent has a specialized role and strict separation of concerns.

```
┌─────────┐    ┌─────────┐    ┌──────────┐    ┌────────┐    ┌──────────┐    ┌──────┐
│ Planner │───▶│ Builder │───▶│ Reviewer  │───▶│ Tester │───▶│ Deployer │───▶│ Docs │
└─────────┘    └─────────┘    └──────────┘    └────────┘    └──────────┘    └──────┘
     │              │              │               │              │             │
     │              │              ▼               │              │             │
     │              │         (rejection)          │              │             │
     │              │              │               │              │             │
     │              ◀──────────────┘               │              │             │
     │                                             │              │             │
     └─────────────────────────────────────────────┴──────────────┴─────────────┘
                            IWO monitors all handoffs
```

## System Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        IWO Daemon (Python)                       │
│                                                                  │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Watchdog  │  │ Headless  │  │ Pipeline │  │    Memory     │  │
│  │ Observer  │  │Commander  │  │ Manager  │  │  Integration  │  │
│  │(filesystem│  │(pane tags,│  │(multi-   │  │(Qdrant+Neo4j) │  │
│  │ monitor)  │  │claude -p, │  │ spec)    │  │              │  │
│  │          │  │model tier)│  │          │  │              │  │
│  └─────┬─────┘  └─────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│  ┌─────┴──────────────┴──────────────┴───────────────┴───────┐  │
│  │                    IWO Daemon Core                         │  │
│  │  - Multi-spec pipeline tracking (PipelineManager)         │  │
│  │  - Per-agent handoff queuing (rejection-first priority)   │  │
│  │  - Safety rails (rejection loops, handoff limits)         │  │
│  │  - Human gates (deploy approval)                          │  │
│  │  - Post-deploy health checks                              │  │
│  │  - Webhook/n8n/ntfy notifications                         │  │
│  │  - Self-healing Ollama (auto-restart on embed failure)    │  │
│  │  - Idempotency tracking                                   │  │
│  │  - Filesystem reconciliation (30s, all spec dirs)         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌───────────────────┐  ┌────────────────────────────────────┐  │
│  │  Metrics Collector │  │        TUI Dashboard (Textual)    │  │
│  │  (Neo4j Cypher    │  │  Agents │ Pipelines │ Metrics      │  │
│  │   aggregation,    │  │  Memory │ Safety    │ Handoff log  │  │
│  │   60s cache)      │  │  Keys: q=quit d=deploy r=refresh   │  │
│  └───────────────────┘  └────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  DirectiveProcessor (operator commands via filesystem)     │  │
│  │  Polls .directives/ every 2s for JSON commands             │  │
│  │  Types: start-spec, next-spec, resume, reconcile, status,  │  │
│  │         pause, unpause, cancel-spec, resolve-ops           │  │
│  │  Archives processed directives to .processed/              │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │                    │                      │
         ▼                    ▼                      ▼
┌─────────────────┐  ┌───────────────┐    ┌──────────────────────┐
│  tmux session    │  │  Filesystem   │    │  External Services   │
│  "claude-agents" │  │  (handoffs)   │    │  (all optional)      │
│                  │  │               │    │                      │
│  Window 0: Plan  │  │  docs/        │    │  Qdrant (vectors)    │
│  Window 1: Build │  │   agent-comms/│    │  Neo4j (graph)       │
│  Window 2: Review│  │    EBATT-022/ │    │  Ollama (embeddings) │
│  Window 3: Test  │  │     001-*.json│    │                      │
│  Window 4: Deploy│  │     002-*.json│    └──────────────────────┘
│  Window 5: Docs  │  │     LATEST -> │
└─────────────────┘  └───────────────┘
```

### Module Structure

```
iwo/
├── __init__.py              # Package init
├── config.py                # Environment-driven configuration (.env + IWO_* vars)
├── parser.py                # Handoff JSON validation (Pydantic models)
├── commander.py             # tmux interaction (pane tagging, legacy dispatch)
├── headless_commander.py    # Headless claude -p dispatch with AGENT_MODEL_MAP
├── directives.py            # DirectiveProcessor (9 directive types, filesystem polling)
├── state.py                 # Agent state (3-state: IDLE/RUNNING/ERROR)
├── pipeline.py              # Multi-spec pipeline tracking + per-agent queuing
├── metrics.py               # Pipeline performance metrics (Neo4j Cypher queries)
├── daemon.py                # Main orchestrator (watchdog, routing, safety, health checks)
├── memory.py                # Qdrant + Neo4j pipeline history storage
├── ops_actions.py           # Ops Actions register (manual infra task tracking)
├── auditor.py               # Agent 007 auditor module (anomaly detection)
└── tui.py                   # Textual TUI dashboard

scripts/
├── directive-next-spec.sh       # Desktop launcher: queue next-spec directive
├── directive-resolve-ops.sh     # Desktop launcher: queue resolve-ops directive
├── launch-tmux-agents.sh        # Parameterized tmux agent launcher
├── setup-new-machine.sh         # Full machine setup (venv, desktop launchers)
├── kanban-dashboard-start.sh    # Start kanban web dashboard
├── kanban-dashboard-stop.sh     # Stop kanban web dashboard
├── kanban-dashboard-restart.sh  # Restart kanban web dashboard
└── setup-credentials.sh         # Bitwarden credential setup

tools/
└── kanban-dashboard.py          # Flask kanban dashboard (localhost:8787)

skills/                          # Bundled agent skills (overridable via IWO_SKILLS_DIR)
├── credential-manager/          # Bitwarden auto-unlock + credential retrieval
└── ops-action-resolver/         # Semi-automated ops action resolution
```

**Total:** ~4,500 lines across 14 Python modules + scripts + skills.

## Configuration (v3.0.0 — Environment-Driven)

All configuration is in `iwo/config.py` as a Python dataclass. Every path and service URL is read from `IWO_*` environment variables, loaded automatically from a `.env` file in the repo root.

### Quick Setup

```bash
cp .env.example .env
# Edit .env with your paths and service URLs
```

### Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `IWO_PROJECT_ROOT` | Yes | `cwd` | Path to the project IWO orchestrates |
| `IWO_LOG_DIR` | No | `{repo}/logs` | Agent log output directory |
| `IWO_SKILLS_DIR` | No | `{repo}/skills` | Agent skills (ops-resolver, credential-mgr) |
| `IWO_TMUX_SESSION` | No | `claude-agents` | tmux session name |
| `IWO_ENABLE_MEMORY` | No | `true` | Enable Qdrant + Neo4j telemetry |
| `IWO_QDRANT_URL` | No | (empty) | Qdrant endpoint. Empty = memory disabled |
| `IWO_QDRANT_API_KEY` | No | (empty) | Qdrant auth key |
| `IWO_NEO4J_URI` | No | (empty) | Neo4j bolt endpoint. Empty = memory disabled |
| `IWO_NEO4J_USER` | No | `neo4j` | Neo4j username |
| `IWO_NEO4J_PASSWORD` | No | (empty) | Neo4j password |
| `IWO_OLLAMA_URL` | No | `http://localhost:11434` | Ollama embedding endpoint |
| `IWO_OLLAMA_MODEL` | No | `mxbai-embed-large` | Ollama embedding model |
| `IWO_NTFY_TOPIC` | No | (empty) | ntfy.sh topic. Empty = notifications disabled |
| `IWO_NTFY_SERVER` | No | `https://ntfy.sh` | ntfy server URL |
| `IWO_WEBHOOK_URL` | No | (empty) | n8n/webhook notification URL |
| `IWO_HEALTH_CHECK_URLS` | No | (empty) | Comma-separated post-deploy health check URLs |
| `IWO_AUTO_APPROVE_SAFE_DEPLOYS` | No | `true` | Auto-approve deploys with no infra changes |
| `IWO_AUTO_DEPLOY_ALL` | No | `false` | Bypass all deploy gates (overnight mode) |
| `IWO_AUTO_CONTINUE` | No | `false` | Auto-queue next spec on pipeline completion |

### Graceful Degradation

IWO is designed to work with minimal configuration. On a fresh clone with no `.env`:

- `project_root` defaults to the current working directory
- `skills_dir` points to the bundled skills in the repo
- Memory auto-disables (no Qdrant/Neo4j endpoints configured)
- Notifications disabled (no ntfy topic configured)
- Health checks disabled (no URLs configured)
- Orchestration works fully — memory and notifications are optional enhancements

### .env Loading

Config loads `.env` via `python-dotenv` (preferred) or a built-in parser (no external dependency required). Environment variables set in the shell always override `.env` values.

## Core Concepts

### Handoff Protocol

Agents communicate via JSON files written to `docs/agent-comms/{SPEC-ID}/`. Each handoff file follows a strict schema:

```json
{
  "metadata": {
    "specId": "EBATT-022",
    "agent": "builder",
    "timestamp": "2026-02-18T18:56:00Z",
    "sequence": 2
  },
  "status": {
    "outcome": "success",
    "goalMet": true,
    "unresolvedIssues": [],
    "reviewFindings": { "blocking": [], "medium": [], "low": [] }
  },
  "deliverables": {
    "filesCreated": ["migrations/0003_battery-tables.sql"],
    "filesModified": ["package.json"],
    "testsStatus": { "passed": 269, "failed": 0, "skipped": 0, "newTests": 33 },
    "typecheckPassed": true
  },
  "nextAgent": {
    "target": "reviewer",
    "action": "Review Sprint 1 deliverables",
    "context": "Focus on SQL schema correctness..."
  }
}
```

Naming convention: `{sequence}-{agent}-{timestamp}.json`. `LATEST.json` is a symlink to the most recent handoff, updated by IWO after processing.

### Headless Dispatch (v2.9.0+)

IWO dispatches work to agents via headless `claude -p` process invocations. Each agent tmux pane operates as an idle bash shell between tasks. When IWO detects a handoff targeting an agent, it launches a fresh `claude -p` process in that pane. When the process exits, the pane returns to idle bash.

Idle detection is deterministic: check `pane_current_command` — if it's `bash`, the agent is idle. No canary probes, no prompt regex matching.

Agent model assignment is tiered via `AGENT_MODEL_MAP`: planner/builder/reviewer use Opus; tester/deployer/docs use Sonnet.

### Directive Processor

The DirectiveProcessor polls `docs/agent-comms/.directives/` every 2 seconds for JSON command files. This enables external control of IWO via filesystem — from desktop launchers, cron, CLI scripts, or Claude Desktop.

Supported directive types: `start-spec`, `next-spec`, `resume`, `reconcile`, `status`, `pause`, `unpause`, `cancel-spec`, `resolve-ops`.

Processed directives are archived to `.directives/.processed/`.

### Ops Actions Register

The Ops Actions system tracks manual infrastructure tasks generated by the pipeline — D1 migrations, wrangler secrets, DNS changes, browser verifications. Actions are auto-extracted from handoff JSON, deduplicated by fingerprint, and stored in `docs/agent-comms/.ops-actions.json`. Priority classification (critical/warning/info) drives ntfy notifications and deploy gate decisions.

### Safety Rails

- Rejection loop detection (>5 rejections between same agent pair halts pipeline)
- Handoff limit (max 150 per spec prevents infinite loops)
- Human gates (deployer requires explicit TUI `d` key approval)
- Agent timeout (30 min no-output triggers STUCK notification)
- Idempotency (each handoff has unique key — duplicates silently ignored)

### Memory Integration (Optional)

IWO stores pipeline telemetry to two external systems. Both are best-effort — if unavailable, IWO continues orchestrating normally.

**Qdrant** (`iwo_pipeline_history` collection, 1024-dim):
- Each handoff gets an embedded summary for semantic search
- Embeddings generated via Ollama mxbai-embed-large
- Enables queries like "find handoffs similar to D1 migration issues"

**Neo4j** (HandoffEvent nodes):
- Structured properties: spec_id, sequence, source/target agent, outcome, timing
- `NEXT_HANDOFF` relationships chain sequential handoffs
- Enables queries like "all specs with rejection loops", "average Builder→Reviewer time"

## Running IWO

### Prerequisites

1. Python 3.11+
2. tmux
3. Claude Code CLI (`claude` command, authenticated)
4. A `.env` file with at minimum `IWO_PROJECT_ROOT` set

### Installation

```bash
git clone https://github.com/No-Smoke/ivans-workflow.git
cd ivans-workflow
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # Edit with your paths
```

### Launch Options

**TUI mode (default):**
```bash
source .venv/bin/activate
iwo-tui
# or: python -m iwo.tui
```

**Headless mode:**
```bash
iwo
# or: python -m iwo.daemon
```

**Desktop launcher:** Run `scripts/setup-new-machine.sh` to install GNOME desktop launchers with right-click actions for directives.

### TUI Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit IWO |
| `d` | Approve pending deploy |
| `r` | Force refresh all agent states |
| `p` | Pause/resume orchestration |
| `a` | Toggle auto-deploy-all (overnight mode) |
| `D` | Toggle auto-continue (overnight mode) |
| `o` | View ops actions panel |

## Troubleshooting

### Agent not picking up a handoff

1. Check IWO processed the file: look for `received_at` in the handoff JSON metadata
2. Check `LATEST.json` symlink points to the correct file
3. Check the agent pane is idle (`pane_current_command` should be `bash`)
4. Check pipeline state: is the agent assigned to a different spec?

### Memory storage failing

Check services: `curl $IWO_OLLAMA_URL/api/tags`, `curl $IWO_QDRANT_URL/collections`. Memory failures are non-fatal — IWO logs warnings and continues.

### Stale pipeline blocking new work

Restart IWO — the fresh `_started_at` timestamp partitions old vs new work.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.5 | 2026-02-18 | Initial "Smart Relay" — watchdog, parser, commander |
| 1.0 | 2026-02-18 | State machine, canary probes, pane tagging, reconciliation |
| 2.0 | 2026-02-18 | TUI dashboard, daemon refactor |
| 2.1 | 2026-02-19 | Memory integration (Qdrant + Neo4j) |
| 2.2 | 2026-02-19 | Enriched parser, pattern library migration 384→1024-dim |
| 2.3 | 2026-02-19 | Multi-spec pipeline: PipelineManager, per-agent queuing |
| 2.4 | 2026-02-19 | Crash recovery, post-deploy health checks |
| 2.5 | 2026-02-19 | Metrics dashboard, webhook/n8n notifications |
| 2.8.0 | 2026-02-21 | Agent 007 auditor module |
| 2.8.5 | 2026-02-21 | Canary-based dispatch, rich activation prompt, 8 bug fixes |
| 2.9.0 | 2026-02-25 | Headless dispatch (`claude -p`), DirectiveProcessor, ops actions register |
| **3.0.0** | **2026-03-17** | **Environment-driven config** — all paths/URLs via `IWO_*` env vars + `.env` file. Zero hardcoded paths. Portable across machines. Public release preparation. |
