# IWO — TUI Dashboard User Manual

**Ivan's Workflow Orchestrator v2.9.0** | Single-Page Reference

---

## Overview

The IWO TUI Dashboard is a live terminal interface that monitors and controls your 7-agent Claude Code workflow. It watches for handoff JSON files, validates them, checks agent readiness via deterministic `pane_current_command` inspection, and automatically dispatches `claude -p` (headless) to the correct agent pane. The dashboard shows agent states, handoff history, and safety rail status in real time.

**Headless dispatch model:** All agent panes start as idle bash shells. When IWO detects a handoff, it launches `claude -p` with the handoff context into the target pane. When claude -p exits, the pane returns to idle bash. No send-keys injection, no interactive prompt detection.

**Agent model tiers:** Each agent is dispatched with an explicit `--model` flag via `AGENT_MODEL_MAP`:

| Tier | Agents | Model | Rationale |
|------|--------|-------|-----------|
| Quality | Planner, Builder, Reviewer | `opus` | Architecture, implementation, and review require deep reasoning |
| Speed | Tester, Deployer, Docs | `sonnet` | Mechanical tasks (run tests, deploy, update docs) benefit from faster execution |

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
2. Give Planner work (interactive `claude` in pane 0, or use desktop launcher "Start Spec" action)
3. Return to the TUI — watch agents cycle through states as IWO dispatches headless `claude -p`
4. When deployer is reached, you get a desktop notification. Press `d` to approve
5. If an agent shows STUCK or WAITING, switch to tmux to intervene manually

## Desktop Launcher Directives

Both IWF and IWO desktop launchers have right-click menu actions that write JSON directive files to `docs/agent-comms/.directives/`. IWO polls this directory every 2 seconds and executes the directive. The "Plan Next Spec" action uses `scripts/directive-next-spec.sh` (standalone script to avoid `.desktop` Exec escaping issues).

| Directive | Requires | Effect |
|-----------|----------|--------|
| next-spec | Optional focus area | Scans completions, selects next logical spec, dispatches Planner with deterministic 10-step prompt |
| start-spec | Spec ID + optional context | Dispatches Planner with spec content |
| resume | Spec ID | Re-dispatches stalled agent from LATEST.json |
| reconcile | — | Triggers filesystem reconciliation |
| status | — | Writes status report + sends ntfy notification |
| pause | — | Stops new dispatches (agents finish current work) |
| unpause | — | Resumes dispatch after pause |
| cancel-spec | Spec ID | Marks pipeline cancelled |

Processed directives are archived to `.directives/.processed/`. Last status report is at `.directives/.last-status.txt`.

**IWO launcher (`iwo.desktop`):** 11 actions — Run Headless, Plan Next Spec, Start Spec, Resume Spec, Reconcile, Status Report, Pause, Unpause, Cancel Spec, Stop IWO, View Logs

**IWF launcher (`boris-workflow.desktop`):** 6 actions — Kill Session, Plan Next Spec, Start Spec, Resume Spec, View Git Log, Load Credentials

## Safety Rails (Automatic)

| Rail | Threshold | Action |
|------|-----------|--------|
| Rejection loop | Same agent-pair >5 times | HALT + desktop notification |
| Handoff limit | >150 per spec | HALT + desktop notification |
| Deploy gate | Any handoff to deployer | HALT — requires `d` key approval |
| Idle detection | `pane_current_command` check | Only dispatches to idle bash shells |
| Duplicate handoff | Same idempotency key | Silently skipped |

---

*IWO v2.9.0 | Headless dispatch + directive system + agent model tiers | github.com/No-Smoke/ivans-workflow-orchestrator | 2026-02-25*
