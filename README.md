# IWO — Ivan's Workflow Orchestrator

Automated handoff routing for Claude Code multi-agent workflows running in tmux.

## What It Does

IWO watches for JSON handoff files written by agents in Ivan's Workflow (derivative of Boris Cherny Workflow) and automatically routes them to the next agent. It replaces you as the manual dispatcher.

**Before IWO:** You read each handoff → switch tmux window → tell agent to proceed (2-5 min/handoff)  
**After IWO:** Daemon watches files → validates → routes → activates next agent (seconds)

## Architecture

Three-model consensus design (Claude Opus + GPT-5.2 + Gemini 3 Pro):

- **watchdog** (inotify) monitors `docs/agent-comms/` for new handoffs
- **pydantic** validates handoff JSON against production schema
- **libtmux** discovers agent panes and sends commands
- **Safety rails**: rejection loop detection, handoff limits, deploy gates
- **Desktop notifications** via `notify-send` for human escalation

## Install

```bash
cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

```bash
# Direct (for testing)
python -m iwo.daemon

# As systemd user service (for persistence)
cp iwo.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iwo.service
journalctl --user -u iwo -f
```

## Configuration

Edit `iwo/config.py` to change:
- `project_root` — path to your project
- `tmux_session_name` — your tmux session name
- `agent_window_map` — agent → window index mapping
- `human_gate_agents` — agents requiring human approval (default: deployer)
- Safety thresholds (rejection loops, timeouts, handoff limits)

## Phases

- **v0.5** (current): Smart relay — watch, validate, route, activate
- **v1.0** (next): State machine, @iwo-agent pane tags, canary probes, cursor position detection
- **v2.0** (future): Dashboard, multi-spec concurrency, AI sidecar

## License

Private — Vanya Davidenko / EthosPower
