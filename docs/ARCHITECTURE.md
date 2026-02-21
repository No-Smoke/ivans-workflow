# Ivan's Workflow Orchestrator (IWO) — Architecture Guide

**Version:** 2.8.5 | **Updated:** 2026-02-21
**Repository:** [No-Smoke/ivans-workflow-orchestrator](https://github.com/No-Smoke/ivans-workflow-orchestrator)

## Overview

Ivan's Workflow Orchestrator (IWO) is a Python daemon that automates handoffs between multiple Claude Code AI agents running in tmux sessions. It monitors for handoff JSON files, validates them, checks agent readiness via a state machine, and routes work to the next agent in a software development pipeline.

IWO is designed for Ivan's Workflow — a six-agent development pipeline where each agent has a specialized role and strict separation of concerns.

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
│  │ monitor)  │  │ 5 states) │  │canary/   │  │              │  │
│  └─────┬─────┘  └─────┬─────┘  │respawn)  │  └──────┬───────┘  │
│        │              │        └────┬─────┘         │          │
│  ┌─────┴──────────────┴──────────────┴───────────────┴───────┐  │
│  │                    IWO Daemon Core                         │  │
│  │  - Multi-spec pipeline tracking (PipelineManager)         │  │
│  │  - Per-agent handoff queuing (rejection-first priority)   │  │
│  │  - Safety rails (rejection loops, handoff limits)         │  │
│  │  - Human gates (deploy approval)                          │  │
│  │  - Post-deploy health checks                              │  │
│  │  - Agent crash recovery (auto-respawn, max 3 attempts)    │  │
│  │  - Webhook/n8n notifications (multi-channel dispatch)     │  │
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
├── commander.py         # tmux interaction (pane tagging, canary, respawn)
├── state.py             # Agent state machine (5 states, polling)
├── pipeline.py          # Multi-spec pipeline tracking + per-agent queuing
├── metrics.py           # Pipeline performance metrics (Neo4j Cypher queries)
├── daemon.py            # Main orchestrator (watchdog, routing, safety, health checks)
├── memory.py            # Qdrant + Neo4j pipeline history storage
└── tui.py               # Textual TUI dashboard

scripts/
└── migrate_patterns_384_to_1024.py  # One-time pattern library migration
```

**Total:** ~3,265 lines across 10 Python modules + migration script.

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
    "deviationsFromPlan": ["Changed implementation approach for..."],
    "reviewFindings": {
      "blocking": [],
      "medium": ["Optional filter not yet implemented"],
      "low": ["Cosmetic: redundant ternary"]
    }
  },
  "deliverables": {
    "filesCreated": ["migrations/0003_battery-tables.sql"],
    "filesModified": ["package.json"],
    "filesReviewed": [],
    "testsStatus": { "passed": 269, "failed": 0, "skipped": 0, "newTests": 33 },
    "typecheckPassed": true
  },
  "evidence": {
    "reviewAreas": { "sqlSchema": "PASS", "sqlSafety": "PASS" },
    "securityCheck": "No injection vectors. All user input via parameterized SQL.",
    "codeQuality": "All functions have explicit return types. No any types."
  },
  "nextAgent": {
    "target": "reviewer",
    "action": "Review Sprint 1 deliverables",
    "context": "Focus on SQL schema correctness...",
    "knownIssues": ["Unused schema fields (Sprint 2 scope)"]
  }
}
```

**Naming convention:** `{sequence}-{agent}-{timestamp}.json` (e.g., `002-builder-2026-02-18T18-56.json`)

**LATEST.json** is a symlink to the most recent handoff, updated by IWO after processing.

### Agent State Machine (Display Only — Not Used for Dispatch)

Each agent runs a 5-state machine, polled every 2 seconds. As of v2.8.5, this is used **only** for the TUI dashboard and auditor alerts. It is NOT in the dispatch critical path — the canary probe is the sole gate for activation decisions.

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

### Agent Activation Flow (Option A — Canary-Based Dispatch)

As of v2.8.5, the state machine is **NOT** in the dispatch critical path. The canary probe is the sole gate for agent activation. The state machine remains for TUI dashboard display and auditor alerts only.

When IWO detects a new handoff file:

```
1. Parse JSON, validate with Pydantic
2. Check idempotency (skip if already processed)
3. Check safety rails (rejection loops, handoff limits)
4. Store to memory (Qdrant + Neo4j, best-effort)
5. Update LATEST.json symlink (IWO owns this, not agents)
6. Stamp received_at in handoff JSON metadata
7. Check human gate (deployer → wait for approval)
8. DIRECT DISPATCH (Layer 1):
   a. Get target agent's tmux pane
   b. Release any stale pipeline assignment on target
   c. Run canary probe (send Enter, wait for prompt in bottom 5 lines)
   d. If canary passes → send rich activation prompt → assign agent
   e. If canary fails → queue handoff for retry
9. QUEUE RETRY (Layer 2, every ~2s poll cycle):
   a. For each agent with queued work:
   b. Skip if pipeline says agent is currently assigned
   c. If queue age < 30s: skip if state machine says PROCESSING
   d. If queue age ≥ 30s: always try canary (override state machine)
   e. If queue age > 120s: send desktop/phone notification
   f. On canary pass → dequeue and activate
```

**Rich activation prompt:** Instead of bare `/workflow-next`, IWO sends a natural language instruction: `"You are the {role} agent. Read the handoff at docs/agent-comms/{spec}/{file} (spec: X, sequence #N, from: Y). Your task: {action}. Execute /workflow-next now — read LATEST.json, activate your role, and begin working immediately. Do NOT just summarize — START EXECUTING."` This is more reliable than slash commands, which Claude Code sometimes silently ignores under context pressure (e.g., when CLAUDE.md exceeds 40k chars).

**Session-based staleness (Option B):** On daemon startup, IWO records `_started_at = time.time()`. During recovery, any handoff file with mtime older than `_started_at` gets its pipeline marked `stale` with no agent assignment. This prevents stale work from previous sessions blocking new dispatches. File mtime is used only for this comparison — it's unreliable for time-based thresholds because reconciliation, agent reads, and other operations touch files.

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
| `ebatt_patterns_v2` | 1024 | 56 | Curated implementation patterns (migrated from 384-dim) | tos-bridge, qdrant-new |
| `ebatt_pattern_library` | 384 | 56 | Legacy patterns (backup, read-only) | Direct API only |
| `project_memory_v2` | 1024 | varies | Session context, decisions | qdrant-new |
| `boris_workflow_skills` | 1024 | 17 | Workflow patterns | tos-bridge, qdrant-new |

**Note:** `ebatt_pattern_library` (384-dim) was migrated to `ebatt_patterns_v2` (1024-dim) on 2026-02-19. The old collection is preserved as backup. All tools now use `ebatt_patterns_v2`.

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
         │    (includes: file counts, test results, review findings, deviations)
         ├──▶ Neo4j: HandoffEvent node + relationships
         │    (enriched: tests_passed/failed, blocking_count, goal_met)
         └──▶ Activate next agent
                  │
                  ▼
         Agent runs /workflow-next
                  │
                  ├──▶ Neo4j: query constraints & ADRs (required for reviewer/deployer)
                  ├──▶ tos-bridge: query boris_workflow_skills (workflow patterns)
                  ├──▶ tos-bridge: query ebatt_patterns_v2 (implementation patterns)
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

### Dispatch Debugging Decision Tree

When an agent doesn't pick up a handoff, follow these steps in order:

**Step 1: Did IWO process the file?**
```bash
python3 -c "import json; d=json.load(open('docs/agent-comms/SPEC/LATEST.json')); print(d.get('metadata',{}).get('received_at','NOT STAMPED'))"
```
- `received_at` present → IWO's watchdog fired and `process_handoff()` ran. Go to Step 2.
- NOT STAMPED → Watchdog didn't fire. Check: Is IWO running (`ps aux | grep iwo`)? Is the file a `.json` (not `.tmp`)? Was it **created** (not moved/renamed) in the watched directory? Check `config.handoffs_dir` matches the actual path.

**Step 2: Is LATEST.json correct?**
```bash
readlink docs/agent-comms/SPEC/LATEST.json
ls -t docs/agent-comms/SPEC/*.json | grep -v LATEST | head -1
```
- If they match → Symlink correct. Go to Step 3.
- If divergent → IWO's symlink update in `on_created()` failed. Check logs for "Failed to update LATEST.json".

**Step 3: Was the canary probe attempted?**
Check TUI log panel for `"Canary probe on {agent} for {spec}..."`. If no canary log:
- `commander.get_agent(target)` returned None → agent pane not discovered. Check tmux session name (`config.tmux_session_name`) and window mapping (`config.agent_window_map`). Verify tmux session exists: `tmux has-session -t claude-agents`.
- Handoff may have been filtered (LATEST.json, .tmp files, 007- prefix, .audit directory are all skipped).

**Step 4: Did canary pass or fail?**
- `"Canary passed"` → Dispatch attempted. Look for `"✅ Activated"` or `"❌ Failed to activate"`. Go to Step 5.
- `"Canary failed"` → Agent not at prompt. Handoff queued. After 30s the retry loop should try again regardless of state machine state. After 2min a notification is sent. If no retry occurs → check `pipeline.is_agent_busy(target)` — if True, a stale assignment is blocking queue drain. Fix: `pipeline.release_agent(target)` or restart IWO.

**Step 5: Did the agent execute the command?**
The rich activation prompt should be visible in the agent's tmux pane. If the text appeared but the agent produced no output:
- Agent's CLAUDE.md may be too large (>40k chars causes unreliable behavior)
- Agent's skill file may not be loaded (check pane for "AGENT INITIALIZED" message)
- Agent may have hit Claude Code's context limit
- Try sending the instruction manually in the tmux pane

**Step 6: Pipeline state inspection**
```bash
# Check what the pipeline manager thinks:
# (from within IWO or via daemon object)
pipeline.agent_current_spec("reviewer")   # What spec is agent assigned to?
pipeline.queue_depth("reviewer")          # How many items queued?
pipeline.get_pipeline("SPEC-ID").status   # active/stale/halted/completed?
```

### Agent shows STUCK (red ⏳) when actually idle

The `idle_prompt_pattern` may not match the agent's prompt. Claude Code uses `❯` (Unicode U+276F). Check with:
```bash
tmux capture-pane -t claude-agents:0 -p | tail -5 | cat -A
```
Note: As of v2.8.5, STUCK state does NOT block dispatch — the canary probe is the definitive check. STUCK only affects the TUI display and auditor alerts.

### Agent shows PROCESSING when actually idle

The state machine detects output changes (cursor movement, status bar redraws, token counter updates) as "activity." Claude Code's TUI continuously updates these elements even when idle. The `_check_idle_prompt()` function mitigates this by checking only the bottom 5 non-empty lines and rejecting if spinner characters (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷) or "Thinking…" are visible. However, false PROCESSING is common and expected — this is why the state machine is not used for dispatch.

### Canary probe times out

IWO sends a bare Enter keystroke and checks if the prompt (`❯` or `>`) reappears in the bottom 5 lines within 10 seconds. Possible causes of timeout:
- Agent is genuinely busy (processing a command)
- Agent is at a confirmation dialog or password prompt
- Agent's Claude Code process has crashed but the pane still exists
- Agent is still initializing (loading CLAUDE.md, skills)
Check the agent pane manually via `tmux select-window -t claude-agents:{N}`.

### Handoff not detected

IWO uses watchdog inotify. If the file was written before IWO started, the recovery scan runs on startup (checks all spec dirs, reconstructs pipeline state). Filesystem reconciliation also runs every 30 seconds. If a file is still missed, restart IWO or press `r` in TUI for manual refresh.

### Stale pipeline blocking new work

If an agent is assigned to a spec from a previous session, the queue retry loop will skip it. On startup, IWO marks pipelines with handoff mtime older than daemon start time as "stale" and releases the agent. If this fails (e.g., file was touched during reconciliation), manually restart IWO — the fresh `_started_at` timestamp will correctly partition old vs new work.

### /workflow-next silently ignored

Claude Code's slash command processing can silently fail when CLAUDE.md exceeds ~40k characters. As of v2.8.5, IWO sends a rich natural language prompt instead of bare `/workflow-next`. If the agent still doesn't act, the LLM may be hitting context limits. Check CLAUDE.md size: `wc -c .claude/CLAUDE.md` (target: under 40,000 chars).

### Memory storage failing

Check services: Ollama (`curl http://localhost:11434/api/tags`), Qdrant (`curl http://74.50.49.35:6333/collections`), Neo4j (`curl http://74.50.49.35:7474`). Memory failures are non-fatal — IWO logs warnings and continues orchestrating.

### No IWO log file for post-mortem

Currently, IWO daemon logs go only to the TUI log panel (Textual RichLog widget). There is no file-based log. For post-mortem debugging, use:
- `received_at` timestamps in handoff JSON files (proves IWO processed them)
- LATEST.json symlink state (proves symlink update worked)
- tmux pane output (`tmux capture-pane -t claude-agents:{N} -p`)
- File modification times: `stat` on handoff files
- **TODO:** Add file handler to IWO logging for persistent post-mortem access.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.5 | 2026-02-18 | Initial "Smart Relay" — watchdog, parser, commander |
| 1.0 | 2026-02-18 | State machine, canary probes, pane tagging, reconciliation |
| 2.0 | 2026-02-18 | TUI dashboard, daemon refactor |
| 2.1 | 2026-02-19 | Memory integration (Qdrant + Neo4j), bugfixes |
| 2.2 | 2026-02-19 | Agent intelligence: enriched parser (deliverables, evidence, review findings), pattern library migration 384→1024-dim, workflow-next context loading (tos-bridge + Neo4j queries with mandatory/best-effort split) |
| 2.3 | 2026-02-19 | Multi-spec pipeline: PipelineManager, per-agent queuing, rejection-first priority, .active-specs.json |
| 2.4 | 2026-02-19 | Operational robustness: crash recovery (auto-respawn), post-deploy health checks, memory health TUI |
| 2.5 | 2026-02-19 | Metrics & observability: pipeline metrics dashboard (Neo4j Cypher), webhook/n8n notification integration |
| 2.5.2+ | 2026-02-19 | Self-healing Ollama (auto-restart on embed failure, Phase 3.0.4) |
| 2.8.0 | 2026-02-21 | Agent 007 auditor module (Phase 3.0 — constitution, schemas, trigger mechanism) |
| 2.8.5 | 2026-02-21 | **Dispatch architecture overhaul:** Option A (canary-based dispatch, state machine removed from critical path), Option B (session-timestamp staleness), rich activation prompt (replaces bare /workflow-next), queue retry with 30s override, 8 bugs fixed |
