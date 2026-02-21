# IWO — TUI Dashboard User Manual

**Ivan's Workflow Orchestrator v2.8.5** | Single-Page Reference

---

## Overview

The IWO TUI Dashboard is a live terminal interface that monitors and controls your 6-agent Claude Code workflow. It watches for handoff JSON files, validates them, checks agent readiness via canary probes, and automatically dispatches a rich activation prompt to the correct agent. The dashboard shows agent states, handoff history, and safety rail status in real time.

**Important:** Agent states shown in the dashboard (IDLE/PROCESSING/STUCK) are for display purposes only. Dispatch decisions use the canary probe exclusively — the state machine is NOT in the dispatch critical path. False PROCESSING is common and expected due to Claude Code TUI status bar redraws.

## Launching

| Method | Command |
|--------|---------|
| TUI Dashboard | `iwo-tui` |
| Headless (no UI) | `iwo` |
| Desktop launcher | Click IWO icon (TUI default, right-click for headless) |
| Custom project | `IWO_PROJECT_ROOT=/path/to/project iwo-tui` |

**Prerequisite:** tmux session `claude-agents` must be running with 6 agent panes.

## Dashboard Layout

```
┌─ IWO — Ivan's Workflow Orchestrator — Phase 2 Dashboard ─┐
│ Spec: PRICING-SINGLE-REPORT │ Uptime: 2h 14m │ HO: 23   │
├──────────────────────────────┬────────────────────────────┤
│ AGENTS                       │ HANDOFF LOG               │
│ ● Planner    IDLE      2m   │ # 23 builder→reviewer ✅  │
│ ◉ Builder    PROCESSING 0s  │ # 22 reviewer→builder ❌  │
│ ● Reviewer   IDLE      45s  │ # 21 planner→builder  ✅  │
│ ● Tester     IDLE      5m   │                           │
│ ● Deployer   IDLE      --   │                           │
│ ● Docs       IDLE      12m  │                           │
├──────────────────────────────┤                           │
│ SAFETY                       │                           │
│ Rejections: 2/5             │                           │
│ Handoffs: 23/150            │                           │
│ Deploy gate: ACTIVE         │                           │
│ Pending: 0                  │                           │
├──────────────────────────────┴────────────────────────────┤
│ 14:23:05 iwo.daemon │ Canary probe on builder for EBATT-006A...     │
│ 14:23:07 iwo.cmd    │ Canary passed for builder — dispatching       │
├───────────────────────────────────────────────────────────┤
│ q Quit │ d Deploy Approve │ r Reconcile │ p Pause/Resume │
└───────────────────────────────────────────────────────────┘
```

| Panel | Shows |
|-------|-------|
| Status Bar | Current spec ID, daemon uptime, total handoff count |
| Agents | Each agent's state (colored indicator), name, and time since last output change |
| Safety | Rejection loop count, handoff count vs limit, deploy gate status, pending queue size |
| Handoff Log | Last 12 handoffs: sequence number, source→target, and outcome (✅/❌) |
| Log Output | Live daemon log stream — state transitions, handoff processing, errors |

## Keyboard Controls

| Key | Action | When to Use |
|-----|--------|-------------|
| `q` | Quit | Clean shutdown — stops observer, exits |
| `d` | Deploy Approve | When deployer is gated — sends `/workflow-next` to deployer |
| `r` | Force Reconcile | Immediate filesystem scan for missed handoffs |
| `p` | Pause / Resume | Toggles state polling and reconciliation on/off |

## Agent States

| Indicator | State | Meaning |
|-----------|-------|---------|
| 🟢 ● | IDLE | Prompt visible, output stable 2s+, cursor stationary. Ready for commands. |
| 🟡 ◉ | PROCESSING | Output changing or cursor moving. Agent is working. |
| 🔴 ⏳ | STUCK | No output for 120s without prompt. May need intervention. |
| 🟣 🙋 | WAITING | Interactive prompt detected (`[Y/n]`, `Password:`, etc). Needs human input. |
| 🔴 💀 | CRASHED | Pane process exited. Restart the agent. |
| ⚪ ○ | UNKNOWN | Initial state before first poll cycle completes. |

## Typical Workflow

1. Launch the TUI: `iwo-tui`
2. Switch to tmux (`claude-agents`) and run `/workflow-start` on the Planner pane
3. Return to the TUI — watch agents cycle through states as IWO relays handoffs
4. When deployer is reached, you get a desktop notification. Press `d` to approve
5. If an agent shows STUCK or WAITING, switch to tmux to intervene manually

## Safety Rails (Automatic)

| Rail | Threshold | Action |
|------|-----------|--------|
| Rejection loop | Same agent-pair >5 times | HALT + desktop notification |
| Handoff limit | >150 per spec | HALT + desktop notification |
| Deploy gate | Any handoff to deployer | HALT — requires `d` key approval |
| Canary probe | 10s timeout | Refuses to send command to unresponsive agent |
| Duplicate handoff | Same idempotency key | Silently skipped |

---

*IWO v2.8.5 | Canary-based dispatch (Option A+B) | github.com/No-Smoke/ivans-workflow-orchestrator | 2026-02-21*
