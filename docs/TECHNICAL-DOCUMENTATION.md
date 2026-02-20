# Ivan's Workflow Orchestrator (IWO) — Technical Documentation

**Version:** 2.8.2 (Phase 3 — Agent 007 Schemas + Constitution)
**Created:** 2026-02-18
**Updated:** 2026-02-21
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
    "outcome": "success|partial|failed|blocked|approved|approved_with_notes|conditional-success",
    "goalMet": true,
    "unresolvedIssues": [],
    "deviationsFromPlan": []
  },
  "nextAgent": {
    "target": "builder|reviewer|tester|deployer|docs|planner|human",
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
2. Scans ALL spec directories under `docs/agent-comms/` (multi-spec pipeline recovery)
3. Parses each handoff JSON and marks them as "already processed" via idempotency keys
4. Rebuilds pipeline state (agent assignments, handoff counts, current agents)
5. **Detects unrouted handoffs** (Phase 2.6): For each spec, checks if the latest handoff's target agent has produced a subsequent handoff. If not, the handoff was never dispatched — it is removed from the "already processed" set and queued for activation after startup completes.
6. Begins watching for new files via watchdog/inotify

This means the daemon can be killed and restarted at any time without losing state. Critically, restarting IWO after a handoff was written but before it was routed will now correctly detect and dispatch that handoff on the next startup.

### Recovery Edge Cases

| Scenario | Behavior |
|----------|----------|
| IWO restarts after handoff written, before routing | Phase 2.6 detects unrouted handoff, queues for activation |
| IWO restarts while agent is mid-work | Target agent shows PROCESSING, handoff queued until IDLE |
| IWO restarts after full pipeline completion | Latest handoff targets "human", no routing needed |
| Multiple specs with pending handoffs | Each detected and queued independently |

---

## 6. Desktop Notifications

IWO uses `notify-send` (libnotify) for human escalation on KDE/GNOME Linux desktops.

Notification types:
- **Normal**: Agent activation confirmations
- **Critical**: Deploy gates, safety rail violations, errors

---

## 6.1 Mobile Notification Pipeline (n8n → ntfy)

For remote/mobile monitoring, the auditor sends webhook events to an n8n workflow on the VPS, which routes them to ntfy for mobile push notifications with severity-based priority.

### Architecture

```
IWO Daemon (auditor.py)
  → POST https://n8n.ethospower.org/webhook/iwo-audit
    → n8n workflow "IWO Audit Event Receiver" (ID: mSKU23aEzwqPJSZy)
      → Switch: route by event.severity
        → HTTP POST to https://ntfy.sh/iwo-vanya-8v8-audit
          → ntfy app on phone (instant delivery, doze bypass)
```

### n8n Workflow Details

- **Workflow ID:** `mSKU23aEzwqPJSZy`
- **Webhook path:** `/webhook/iwo-audit`
- **Nodes:** 7 (Webhook → Switch → 4× HTTP Request → Respond OK)
- **VPS:** 74.50.49.35 / n8n.ethospower.org
- **n8n container:** `/opt/n8n/docker-compose.yml`

### Severity → Priority Mapping

| Severity | ntfy Priority | Emoji Tag | Phone Behavior |
|----------|---------------|-----------|----------------|
| info | low (2) | ℹ️ | Silent, badge only |
| warning | default (3) | ⚠️ | Normal sound |
| critical | high (4) | 🚨 | Loud, LED flash |
| fatal | urgent (5) | 💀 | Alarm, bypasses DND |

### Webhook Payload Schema

```json
{
  "event": {
    "timestamp": "ISO-8601",
    "check": "check_name",
    "severity": "info|warning|critical|fatal",
    "spec_id": "SPEC-ID or null",
    "details": {"message": "Human-readable description", ...},
    "action_taken": "none|monitoring|blocked_deployment|...",
    "recommended_action": "What the operator should do"
  },
  "daemon_version": "2.8.0"
}
```

### MCP Tooling for n8n

The `mcp-n8n-builder` MCP server (spences10) provides CRUD + activation for n8n workflows from Claude Desktop. A wrapper script at `/home/vanya/scripts/mcp-n8n-builder-wrapper.sh` patches a Zod v3/v4 compatibility issue in the package. The `N8N_HOST` env var must be set to `https://n8n.ethospower.org` (the MCP appends `/api/v1` automatically).

---

### 6.2 Agent 007 — AI Pipeline Supervisor (Phase 3, in progress)

Agent 007 is a Claude Code agent (Opus 4.6) in tmux window 6 that diagnoses and retries stalled pipelines. It is NOT a normal pipeline agent — it is activated on-demand by the auditor when anomalies exceed retry-safe thresholds.

**Activation model:** On-demand. Pane 7 starts as bash shell. IWO daemon launches Claude Code with structured diagnostic prompt when triggered. 007 runs, diagnoses, acts, writes report, exits. Pane returns to idle.

**Safety rails:** 12 forbidden actions (cannot modify `iwo/`, `.claude/`, `CLAUDE.md`, `src/`, cannot deploy, cannot kill panes, max 3 retries per failure). Constitution at `.claude/skills/agent-007-supervisor/SKILL.md` in eBatt repo.

**Retry-safe checks:** Only `agent_liveness`, `agent_timeout`, `stale_assignment` trigger 007. All other checks escalate directly to human.

**Schemas (v1, strict — additionalProperties: false everywhere):**

| Schema | Path | Purpose |
|--------|------|---------|
| Retry Handoff | `iwo/schemas/retry_handoff.json` | Written by 007 when retrying a stalled agent. Distinct naming (`007-retry-{N}.json`) prevents normal handoff parser from processing it. |
| Diagnostic Report | `iwo/schemas/diagnostic_report.json` | Written after every 007 activation. Full audit trail: trigger, diagnosis, actions, outcome, duration. |
| Completion Signal | `iwo/schemas/completion_signal.json` | Lightweight file written at exit. Daemon watches for this to detect 007 completion. |

**File naming conventions:**
- Retry handoffs: `docs/agent-comms/{SPEC-ID}/007-retry-{N}.json`
- Diagnostic reports: `docs/agent-comms/.audit/007-{TIMESTAMP}.json`
- Completion signals: `docs/agent-comms/.audit/007-complete-{TIMESTAMP}.json`

**Decision protocol:** Capture evidence (tmux pane output + handoffs) → Classify failure (transient/deterministic/configuration/stall/unknown) → Act (retry if transient/stall and count < 3, else escalate) → Report (diagnostic JSON + webhook to ntfy) → Exit.

**Remaining Phase 3 deliverables:**
- D2: Diagnosis skill file (deferred to after D4 implementation)
- D3: tmux window 6 in launch script
- D4: Auditor → 007 trigger mechanism in daemon.py/commander.py
- D7/D8: Integration tests

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

### Phase 0.5 (Complete) — Smart Relay
- watchdog file watching
- Pydantic validation
- libtmux agent discovery by window index
- Safety rails (rejection loops, handoff limits, deploy gate)
- Idempotency tracking
- Desktop notifications
- Stateless crash recovery

### Phase 1.0 (Current) — State Machine
- @iwo-agent pane tagging (replaces window index, survives rearrangement)
- Agent state machine: IDLE → PROCESSING → STUCK → WAITING_HUMAN → CRASHED
- Canary probe before command injection (echo + wait for echo)
- Cursor position check (phantom prompt prevention)
- Pending activation queue (waits for target IDLE before sending commands)
- pipe-pane archival logging
- 30-second periodic filesystem reconciliation
- WAITING_HUMAN detection patterns (`[Y/n]`, `Password:`, `CONFLICT`, `--More--`)
- State polling every 2s with desktop notifications for state changes

### Phase 2.0 (Complete) — TUI Dashboard
- Textual-based terminal UI with live-updating widgets
- Agent state panel with colored indicators and time-since-change
- Handoff history log (last 12 handoffs with outcome icons)
- Safety rails panel (rejection count, handoff count, deploy gate, pending)
- Live log output panel routing all IWO logging
- Keybindings: q=quit, d=deploy approve, r=force reconcile, p=pause/resume
- Desktop launcher defaults to TUI mode (headless available via right-click)
- Status bar showing current spec, uptime, total handoffs
- Deploy gate manual approval via 'd' key

### Phase 2.3.1 (Complete) — Multi-Spec Pipeline
- Multi-spec concurrent pipeline tracking with per-agent assignment
- Per-agent handoff queuing with rejection-first priority
- Pipeline lifecycle (active → completed | halted)
- `.active-specs.json` state persistence
- Recovery from filesystem for all specs (not just `.current-spec`)

### Phase 2.7 (Complete) — Pipeline Completion + Supersede + Timestamps
- Terminal target handling: handoffs targeting "human" or "none" now mark the pipeline as completed instead of attempting (and failing) to activate a nonexistent tmux pane. Prevents infinite re-queue loop.
- Pipeline completion state: `.active-specs.json` now shows `status: "completed"` for finished pipelines. Recovery marks pipelines complete when latest (or any prior) handoff targets human/none. Stale agent assignments cleared on completion.
- Recovery queue inflation fix: pipelines that ever reached a terminal state are skipped during unrouted handoff detection, preventing false-queuing of historical handoffs.
- Handoff supersede: when a second file arrives with the same idempotency key (same agent, same sequence) but a different filename, it supersedes the first. Enables Reviewer redos where the improved version actually gets routed.
- Canonical timestamps: IWO stamps `metadata.received_at` (actual UTC) on every handoff file upon receipt. Agent-authored timestamps are unreliable (LLMs fabricate plausible times). Templates updated to instruct agents to run `date -u` for timestamps.
- Stale agent assignments: `recover_from_handoffs()` no longer assigns "human" or "none" to the agent-spec mapping.
- **Multi-sprint fix (v2.7.1):** Removed `ever_completed` check that permanently marked specs as completed if ANY historical handoff targeted human/none. This broke multi-sprint specs (e.g., AI-INFRASTRUCTURE Sprint 3 blocked because Sprint 1's docs→human existed). Now only the LATEST handoff determines terminal state. Added pipeline reactivation: `record_handoff()` resets completed pipelines to active when new handoffs arrive. Added 24-hour age guard on unrouted handoff recovery to prevent stale files from being force-dispatched.

### Phase 2.8 (Complete) — Auditor Module (Agent 007 Phase 1)

Deterministic health monitoring integrated into the daemon poll loop. The auditor runs 8 checks: agent liveness (30-min warning), agent timeout (60-min critical), pipeline consistency (auto-fixes halted pipelines that receive success handoffs), sequence continuity (detects gaps in handoff numbering), timestamp sanity (detects fabricated agent timestamps), stale assignment (auto-releases agents from completed/halted pipelines), queue inflation (warns at depth >5), and daemon heartbeat (writes `heartbeat.json` every 60s for external monitoring).

Integration: `auditor.post_handoff_checks(handoff)` runs after each handoff is routed (step 11 in `process_handoff()`). `auditor.periodic_checks()` runs every poll tick but self-throttles to 5-minute intervals. Heartbeat runs on its own 60-second interval. All calls are best-effort with try/except — auditor failure never crashes the daemon.

Output: diagnostic events written to `docs/agent-comms/.audit/{timestamp}_{check}.json`, webhook POST to n8n on warnings and above, desktop notifications on critical and above.

Files: `iwo/auditor.py` (616 lines), `tests/test_auditor.py` (559 lines, 29 tests).

### Phase 3.0 — Multi-Project and Remote
- Multi-project support (multiple project roots with separate handoff directories)
- AI sidecar (local model for handoff quality checking)
- Telegram/Signal notifications for remote operation
- textual-web for browser-based remote access

---

## 10. Hardware Requirements

IWO itself is lightweight (~50MB RAM, no GPU). It runs alongside the agents on the same machine.

**Current deployment:** Intel NUC 9 Extreme (i9, 64GB RAM, RTX 3060 12GB, Ubuntu/KDE)
**Future deployment:** Dell Precision 5820 (Xeon W-2133, 64GB ECC, RTX 3070 8GB, Proxmox)

On Proxmox, IWO will run in a lightweight LXC container (~256MB RAM allocation).
