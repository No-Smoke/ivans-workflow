# Ivan's Workflow Orchestrator (IWO) — Architecture Guide

**Version:** 2.1.0 | **Updated:** 2026-02-19
**Repository:** [No-Smoke/ivans-workflow-orchestrator](https://github.com/No-Smoke/ivans-workflow-orchestrator)

## Overview

Ivan's Workflow Orchestrator (IWO) is a Python daemon that automates handoffs between multiple Claude Code AI agents running in tmux sessions. It monitors for handoff JSON files, validates them, checks agent readiness via a state machine, and routes work to the next agent in a software development pipeline.

IWO is designed for the "Boris Cherny Workflow" — a six-agent development pipeline where each agent has a specialized role and strict separation of concerns.

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
│  │ Watchdog  │  │   State   │  │  Tmux    │  │    Memory     │  │
│  │ Observer  │  │  Machine  │  │Commander │  │  Integration  │  │
│  │(filesystem│  │(per-agent │  │(pane tags│  │(Qdrant+Neo4j) │  │
│  │ monitor)  │  │ 5 states) │  │ canary)  │  │              │  │
│  └─────┬─────┘  └─────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│        │              │              │               │          │
│  ┌─────┴──────────────┴──────────────┴───────────────┴───────┐  │
│  │                    IWO Daemon Core                         │  │
│  │  - Handoff parsing & validation (Pydantic)                │  │
│  │  - Safety rails (rejection loops, handoff limits)         │  │
│  │  - Human gates (deploy approval)                          │  │
│  │  - Idempotency tracking                                   │  │
│  │  - Filesystem reconciliation (30s)                        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    TUI Dashboard (Textual)                │  │
│  │  Agent states │ Handoff log │ Safety rails │ Live log     │  │
│  │  Keybindings: q=quit d=deploy r=refresh p=pause          │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │                    │                      │
         ▼                    ▼                      ▼
┌─────────────────┐  ┌───────────────┐    ┌──────────────────────┐
│  tmux session    │  │  Filesystem   │    │  External Services   │
│  "claude-agents" │  │  (handoffs)   │    │                      │
│                  │  │               │    │  Qdrant (VPS:6333)   │
│  Window 0: Plan  │  │  docs/        │    │  Neo4j (VPS:7687)    │
│  Window 1: Build │  │   agent-comms/│    │  Ollama (local:11434)│
│  Window 2: Review│  │    EBATT-022/ │    │                      │
│  Window 3: Test  │  │     001-*.json│    └──────────────────────┘
│  Window 4: Deploy│  │     002-*.json│
│  Window 5: Docs  │  │     LATEST -> │
└─────────────────┘  └───────────────┘
```

### Module Structure

```
iwo/
├── __init__.py          # Package init
├── config.py            # Central configuration (dataclass)
├── parser.py            # Handoff JSON validation (Pydantic models)
├── commander.py         # tmux interaction (pane tagging, canary, commands)
├── state.py             # Agent state machine (5 states, polling)
├── daemon.py            # Main orchestrator (watchdog, routing, safety)
├── memory.py            # Qdrant + Neo4j pipeline history storage
└── tui.py               # Textual TUI dashboard
```

**Total:** ~1,750 lines across 8 Python modules.

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
    "deviationsFromPlan": []
  },
  "deliverables": {
    "filesCreated": ["migrations/0003_battery-tables.sql"],
    "filesModified": ["package.json"],
    "testsStatus": { "passed": 269, "failed": 0 }
  },
  "nextAgent": {
    "target": "reviewer",
    "action": "Review Sprint 1 deliverables",
    "context": "Focus on SQL schema correctness..."
  }
}
```

**Naming convention:** `{sequence}-{agent}-{timestamp}.json` (e.g., `002-builder-2026-02-18T18-56.json`)

**LATEST.json** is a symlink to the most recent handoff, updated by IWO after processing.

### Agent State Machine

Each agent runs a 5-state machine, polled every 2 seconds:

```
                 ┌──────────┐
        ┌───────▶│  UNKNOWN  │◀──── initial state
        │        └─────┬─────┘
        │              │ first poll
        │              ▼
        │        ┌──────────┐     output changes
        │   ┌───▶│   IDLE   │────────────────┐
        │   │    └──────────┘                 │
        │   │         ▲                       ▼
        │   │         │ stable        ┌──────────────┐
        │   │         │ + prompt      │  PROCESSING  │
        │   │         └───────────────┤              │
        │   │                         └──────┬───────┘
        │   │                                │
        │   │    ┌──────────┐                │ no output for
        │   │    │  STUCK   │◀───────────────┘ 600 seconds
        │   │    └──────────┘
        │   │
        │   │    ┌──────────────┐
        │   └────│WAITING_HUMAN │◀── [Y/n], Password:, etc.
        │        └──────────────┘
        │
        │        ┌──────────┐
        └────────│ CRASHED  │◀── pane process exited
                 └──────────┘
```

**Idle detection:** IWO scans all terminal lines for the Claude Code prompt character (`❯` or `>`). The prompt must be stable for 2 seconds with no output changes.

**Canary probe:** Before activating an agent, IWO sends a bare Enter keystroke and verifies the prompt reappears. This confirms the agent is responsive (not hung or in a broken state).

### Safety Rails

- **Rejection loop detection:** If the same agent pair rejects > 5 times, IWO halts and notifies
- **Handoff limit:** Max 150 handoffs per spec (prevents infinite loops)
- **Human gates:** Deployer requires explicit approval (TUI `d` key)
- **Agent timeout:** 30 minutes of no output triggers STUCK notification
- **Idempotency:** Each handoff has a unique key (`{specId}:{sequence}:{source}:{target}`) — duplicates are silently ignored

### Agent Activation Flow

When IWO detects a new handoff file:

```
1. Parse JSON, validate with Pydantic
2. Check idempotency (skip if already processed)
3. Check safety rails (rejection loops, handoff limits)
4. Store to memory (Qdrant + Neo4j, best-effort)
5. Update LATEST.json symlink
6. Check human gate (deployer → wait for approval)
7. Check target agent state:
   - IDLE → canary probe → send /workflow-next → mark PROCESSING
   - PROCESSING/UNKNOWN → queue for later (pending activations)
   - STUCK/CRASHED/WAITING_HUMAN → queue + notify
8. Pending activations checked every 2s during state polling
```

### Memory Integration

IWO stores pipeline telemetry to two systems:

**Qdrant** (`iwo_pipeline_history` collection, 1024-dim Cosine):
- Each handoff gets an embedded summary for semantic search
- Embeddings generated via Ollama mxbai-embed-large (same model as tos-bridge)
- Enables queries like "find handoffs similar to D1 migration issues"

**Neo4j** (HandoffEvent nodes):
- Structured properties: spec_id, sequence, source/target agent, outcome, timing
- `NEXT_HANDOFF` relationships chain sequential handoffs
- `HAS_HANDOFF` links to existing Specification nodes
- Enables queries like "all specs with rejection loops", "average Builder→Reviewer time"

Memory is **best-effort**: if Qdrant, Neo4j, or Ollama is unavailable, IWO continues orchestrating normally. Failures are logged but never interrupt the pipeline.

## Memory Ecosystem

IWO integrates into a broader memory architecture shared across multiple tools:

### Qdrant Collections (VPS: 74.50.49.35:6333)

| Collection | Dimensions | Points | Purpose | Access Via |
|------------|-----------|--------|---------|------------|
| `iwo_pipeline_history` | 1024 | 0+ | Handoff telemetry, pipeline history | IWO daemon, qdrant-new |
| `ebatt_pattern_library` | 384 | 56 | Curated implementation patterns | tos-bridge ONLY |
| `project_memory_v2` | 1024 | varies | Session context, decisions | qdrant-new |
| `boris_workflow_skills` | 1024 | 17 | Workflow patterns | qdrant-new |

**Important:** `ebatt_pattern_library` uses 384-dim embeddings (legacy). It cannot be searched via qdrant-new (1024-dim). The canonical access path is **tos-bridge**, which has its own 384-dim embedding model.

### Neo4j Graph (VPS: 74.50.49.35:7687)

Two MCP interfaces hit the **same database**:

| Interface | Tool Name | Purpose | Use For |
|-----------|-----------|---------|---------|
| Entity CRUD | `neo4j-memory-remote` | High-level create/search/delete | Ad-hoc memory storage, simple lookups |
| Raw Cypher | `neo4j-mcp-remote` | Full query power | Complex queries, aggregations, graph traversals |

Key node types relevant to IWO:
- `HandoffEvent` — created by IWO memory module
- `Specification` (151 nodes) — linked to HandoffEvents via HAS_HANDOFF
- `Pattern` (52 nodes) — synced from pattern library via tos-bridge
- `ADR` (11 nodes) — architecture decision records

### tos-bridge MCP Server

Purpose-built bridge between Qdrant and Neo4j. Connected to both Claude.ai and Claude Code.

- **Embedding model:** Ollama mxbai-embed-large (1024-dim)
- **Qdrant endpoint:** http://74.50.49.35:6333
- **Neo4j endpoint:** bolt://74.50.49.35:7687
- **Key tools:** `search_with_graph`, `store_doc_with_graph`, `sync_to_tos`
- **Repository:** [No-Smoke/tos-bridge](https://github.com/No-Smoke/tos-bridge)
- **Local path:** ~/Nextcloud/PROJECTS/tos-bridge/

### Data Flow

```
Agent writes handoff JSON
         │
         ▼
IWO daemon detects file (watchdog)
         │
         ├──▶ Qdrant: embed summary → iwo_pipeline_history
         ├──▶ Neo4j: HandoffEvent node + relationships
         └──▶ Activate next agent
                  │
                  ▼
         Agent runs /workflow-next
                  │
                  ├──▶ tos-bridge: query pattern library (via Claude Code MCP)
                  └──▶ Begin work with historical context
```

## Configuration

All configuration is in `iwo/config.py` as a Python dataclass:

```python
@dataclass
class IWOConfig:
    # Paths
    project_root: Path          # eBatt project root
    handoffs_dir: Path          # docs/agent-comms/
    log_dir: Path               # IWO logs

    # tmux
    tmux_session_name: str = "claude-agents"
    agent_window_map: dict      # agent → window index

    # Safety
    max_rejection_loops: int = 5
    max_handoffs_per_spec: int = 150
    agent_timeout_seconds: int = 1800
    human_gate_agents: set = {"deployer"}

    # State machine
    state_poll_interval_seconds: float = 2.0
    output_stable_seconds: float = 2.0
    stuck_timeout_seconds: float = 600.0
    idle_prompt_pattern: str = r"[❯>]\s*$"

    # Memory (Phase 2.1)
    enable_memory: bool = True
    qdrant_url: str = "http://74.50.49.35:6333"
    neo4j_uri: str = "bolt://74.50.49.35:7687"
    ollama_url: str = "http://localhost:11434"
    ollama_embed_model: str = "mxbai-embed-large"
```

Override the project root via environment variable: `IWO_PROJECT_ROOT=/path/to/project`

## Running IWO

### Prerequisites

1. tmux session named `claude-agents` with 6 windows (launched by Boris workflow script)
2. Ollama running locally with `mxbai-embed-large` model (for memory integration)
3. Qdrant and Neo4j accessible at configured URLs (optional — memory degrades gracefully)

### Launch Options

**TUI mode (default):**
```bash
cd ~/Nextcloud/PROJECTS/ivans-workflow-orchestrator
python3 -m iwo.tui
```

**Headless mode:**
```bash
python3 -m iwo.daemon
```

**Desktop launcher:** `Ivan's Workflow` in GNOME application menu (default: TUI mode, right-click for headless or kill session).

### TUI Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit IWO |
| `d` | Approve pending deploy |
| `r` | Force refresh all agent states |
| `p` | Pause/resume orchestration |

## Troubleshooting

### Agent shows STUCK (red ⏳) when actually idle

The `idle_prompt_pattern` may not match the agent's prompt. Claude Code uses `❯ ` (Unicode U+276F). Check with:
```bash
tmux capture-pane -t claude-agents:0 -p | tail -5 | cat -A
```

### Canary probe times out

IWO sends a bare Enter keystroke and checks if the prompt reappears. If the agent is in a state where Enter triggers an action (e.g., confirmation dialog), the canary may behave unexpectedly. Check the agent manually.

### Handoff not detected

IWO uses watchdog inotify. If the file was written before IWO started, use filesystem reconciliation (runs every 30s) or restart IWO to trigger recovery scan.

### Memory storage failing

Check that Ollama is running (`curl http://localhost:11434/api/tags`), Qdrant is reachable (`curl http://74.50.49.35:6333/collections`), and Neo4j is up (`curl http://74.50.49.35:7474`). Memory failures are non-fatal — IWO logs warnings and continues.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.5 | 2026-02-18 | Initial "Smart Relay" — watchdog, parser, commander |
| 1.0 | 2026-02-18 | State machine, canary probes, pane tagging, reconciliation |
| 2.0 | 2026-02-18 | TUI dashboard, daemon refactor |
| 2.1 | 2026-02-19 | Memory integration (Qdrant + Neo4j), bugfixes |
