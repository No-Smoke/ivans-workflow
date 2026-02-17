# Ivan's Workflow Orchestrator (IWO) — Technical Documentation

**Version:** 0.5.0 (Phase 0.5 — Smart Relay)
**Created:** 2026-02-18
**Author:** Three-model consensus design (Claude Opus 4.6 + GPT-5.2 + Gemini 3 Pro)
**Repository:** https://github.com/No-Smoke/ivans-workflow-orchestrator

---

## 1. Overview

IWO is a Python daemon that automates handoffs between Claude Code CLI agents running in Ivan's Workflow (a derivative of the Boris Cherny Workflow). It replaces the human operator as the dispatcher between 6 specialized AI agents — Planner, Builder, Reviewer, Tester, Deployer, and Docs — that communicate through structured JSON handoff files on the filesystem.

### Problem Solved

Without IWO, each handoff requires manual intervention: reading the handoff JSON, switching tmux windows, and instructing the next agent to proceed. This takes 2-5 minutes per handoff and cannot happen while the operator is away. Complex specs like MONETIZATION-MVP generated 108+ handoffs over 3 days, representing 4-8 hours of pure dispatching overhead.

### Design Principles

The orchestrator is deliberately "dumb" — it is a deterministic state machine, not an AI agent. All three consulted models independently reached this conclusion:

1. **Keep intelligence in the agents, keep the daemon dumb** — routing logic is deterministic (the handoff JSON already specifies `nextAgent.target`)
2. **Filesystem as source of truth** — the daemon is stateless and can recover from crashes by scanning handoff directories
3. **Human remains the exception handler** — IWO dispatches routine handoffs; humans handle deploy approvals, rejection loops, and structural issues

### Architecture Origin

The design was produced through a structured multi-model consultation:

- **Claude Opus 4.6** — initial architecture, handoff protocol analysis, safety rails
- **GPT-5.2** — failure mode analysis, tmux fragility mitigations, interactive prompt detection patterns, systemd recommendation
- **Gemini 2.5 Pro** — atomic write pattern, orchestrator owns LATEST.json, stateless recovery, single-spec V1
- **Gemini 3 Pro** — stress testing, cursor position check, @iwo-agent pane tagging, canary probe, libtmux, V0.5 approach

---

## 2. Architecture

```
┌─────────────────────────────────────────────────┐
│                 IWO DAEMON                       │
│  watchdog ──▶ pydantic ──▶ safety ──▶ libtmux  │
│  (inotify)    (validate)   (rails)    (route)   │
└──────────────────────┬──────────────────────────┘
                       │ /workflow-next
                       ▼
┌─────────────────────────────────────────────────┐
│  tmux: claude-agents                             │
│  %0:Planner  %1:Builder  %2:Reviewer            │
│  %3:Tester   %4:Deployer %5:Docs                │
└─────────────────────────────────────────────────┘
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| Config | `iwo/config.py` | Paths, thresholds, agent mapping |
| Parser | `iwo/parser.py` | Pydantic models for handoff JSON validation |
| Commander | `iwo/commander.py` | libtmux agent discovery, capture-pane, command injection |
| Daemon | `iwo/daemon.py` | Main loop: watchdog → parse → validate → route → activate |

### Dependencies

- `libtmux>=0.37.0` — Python API for tmux interaction
- `watchdog>=4.0.0` — Filesystem monitoring via inotify
- `pydantic>=2.0.0` — JSON schema validation

---

## 3. Handoff Protocol

Agents write JSON handoff files to `docs/agent-comms/{SPEC-ID}/`. The critical routing field is `nextAgent.target`.

### Production Handoff Schema

```json
{
  "metadata": {
    "specId": "LANDING-PAGE-BG",
    "agent": "reviewer",
    "timestamp": "2026-02-16T21:30:00Z",
    "sequence": 7
  },
  "status": {
    "outcome": "success|failed",
    "issueCount": 0,
    "claimMismatches": 0
  },
  "nextAgent": {
    "target": "tester|builder|deployer|docs|planner",
    "action": "Description of what the next agent should do",
    "context": "Additional context for the agent"
  }
}
```

### Routing Rules

| Source | Outcome | Target | Direction |
|--------|---------|--------|-----------|
| Planner | success | Builder | Forward |
| Builder | success | Reviewer | Forward |
| Reviewer | success | Tester | Forward |
| Reviewer | failed | Builder | **Backward** |
| Tester | success | Deployer | Forward |
| Tester | failed | Builder | **Backward** |
| Deployer | success | Docs | Forward |
| Docs | success | (complete) | Terminal |

### File Naming Convention

```
{sequence}-{agent}-{timestamp}.json
Example: 007-reviewer-2026-02-16T21-30-00Z.json
```

### LATEST.json

IWO owns this file. After validating a new handoff, IWO creates a symlink:
```
LATEST.json → 007-reviewer-2026-02-16T21-30-00Z.json
```
Agents read LATEST.json via `/workflow-next` but never write it.

---

## 4. Safety Rails

| Rail | Threshold | Action |
|------|-----------|--------|
| Rejection loop | Same agent-pair >5 times | HALT + desktop notification |
| Total handoffs | >150 per spec | HALT + desktop notification |
| Deploy gate | Always for deployer | HALT + notification, requires human |
| Invalid JSON | Parse failure | HALT + notification |
| Missing fields | Pydantic validation error | HALT + notification |
| Duplicate handoff | Same idempotency key | Skip silently |

### Idempotency

Each handoff is identified by a composite key: `{spec_id}:{sequence}:{source_agent}:{target_agent}`. This prevents duplicate processing if inotify fires multiple events for the same file.

### Deploy Gate

The deployer agent is in `human_gate_agents` by default. When a handoff targets the deployer, IWO sends a desktop notification but does NOT activate the agent. The human must manually proceed.

---

## 5. Crash Recovery

IWO is stateless. On startup it:

1. Reads `.current-spec` to find the active specification
2. Scans all existing handoff JSONs in that spec's directory
3. Parses each and marks them as "already processed" via idempotency keys
4. Begins watching for new files

This means the daemon can be killed and restarted at any time without losing state or re-triggering old handoffs.

---

## 6. Desktop Notifications

IWO uses `notify-send` (libnotify) for human escalation on KDE/GNOME Linux desktops.

Notification types:
- **Normal**: Agent activation confirmations
- **Critical**: Deploy gates, safety rail violations, errors

---

## 7. Installation

```bash
cd ~/Nextcloud/PROJECTS/ivans-workflow-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Running Directly

```bash
python -m iwo.daemon
```

### Running as systemd User Service

```bash
cp iwo.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iwo.service
journalctl --user -u iwo -f
```

### Prerequisites

- Ivan's Workflow must be running (`tmux session: claude-agents` with 6 agent windows)
- Python 3.11+
- Linux with inotify support

---

## 8. Configuration

All configuration is in `iwo/config.py`. Key settings:

| Setting | Default | Purpose |
|---------|---------|---------|
| `project_root` | `~/Nextcloud/PROJECTS/ebatt-ai/ebatt` | Path to project using Ivan's Workflow |
| `tmux_session_name` | `claude-agents` | tmux session containing agent panes |
| `agent_window_map` | `{planner:0, builder:1, ...}` | Agent → window index mapping |
| `human_gate_agents` | `{deployer}` | Agents requiring human approval |
| `max_rejection_loops` | 5 | Max same-pair rejections before HALT |
| `max_handoffs_per_spec` | 150 | Max handoffs per spec before HALT |
| `agent_timeout_seconds` | 1800 | 30 min timeout for stuck agents |
| `file_debounce_seconds` | 1.5 | Wait after file creation before reading |

Override `project_root` via environment variable:
```bash
IWO_PROJECT_ROOT=/path/to/other/project python -m iwo.daemon
```

---

## 9. Roadmap

### Phase 0.5 (Current) — Smart Relay
- watchdog file watching
- Pydantic validation
- libtmux agent discovery by window index
- Safety rails (rejection loops, handoff limits, deploy gate)
- Idempotency tracking
- Desktop notifications
- Stateless crash recovery

### Phase 1.0 — State Machine
- @iwo-agent pane tagging (replaces window index)
- Agent state machine: IDLE → PROCESSING → STUCK → WAITING_HUMAN → CRASHED
- Canary probe before command injection
- `IWO_READY>` forced prompt for deterministic idle detection
- Cursor position check (phantom prompt prevention)
- pipe-pane archival logging
- 30-second periodic reconciliation
- WAITING_HUMAN detection patterns (`[Y/n]`, `Password:`, `CONFLICT`, `--More--`)

### Phase 2.0 — Dashboard and Concurrency
- Rich terminal UI or FastAPI web dashboard
- Multi-spec concurrent pipeline with agent locking
- AI sidecar (local model for handoff quality checking)
- Telegram/Signal notifications for remote operation

---

## 10. Hardware Requirements

IWO itself is lightweight (~50MB RAM, no GPU). It runs alongside the agents on the same machine.

**Current deployment:** Intel NUC 9 Extreme (i9, 64GB RAM, RTX 3060 12GB, Ubuntu/KDE)
**Future deployment:** Dell Precision 5820 (Xeon W-2133, 64GB ECC, RTX 3070 8GB, Proxmox)

On Proxmox, IWO will run in a lightweight LXC container (~256MB RAM allocation).
