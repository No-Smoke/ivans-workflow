# FIX: IWO Notification Storm Causing GNOME UI Freezes

**Date:** 2026-02-22
**Severity:** High — caused system-wide UI lag and freeze/unfreeze cycles
**Files changed:** `iwo/config.py`, `iwo/daemon.py`, `iwo/tui.py`

## Problem

GNOME Shell was consuming 23% CPU and 1.68GB RSS, causing periodic UI freezes. The root cause was IWO's notification system flooding GNOME's D-Bus notification daemon via `notify-send`.

### Mechanism

1. `_poll_agent_states()` runs every 2s (TUI timer), calling `_process_pending_activations()` at the end of each tick
2. When a handoff is queued and `queue_age > 30s`, a canary probe fires on every tick regardless of state machine state
3. The canary probe takes up to 10s (20 tmux `capture-pane` calls at 0.5s intervals), but the 2s timer keeps firing, causing probe stacking
4. When `queue_age > 120s`, every failed canary triggers `_notify()` which spawns `subprocess.run(["notify-send", ...])` — a D-Bus call to `org.freedesktop.Notifications`
5. GNOME Shell renders a notification bubble for each call (CSS animation + compositor recomposition + D-Bus round-trip), at ~1 notification every 2 seconds
6. Continuous animation work drove gnome-shell to 23% CPU and caused frame drops perceived as UI freezes

### System state at diagnosis

- 809 total processes, 83 node processes, 8 Electron processes
- 6 Claude Code instances: 3.3GB RAM, ~30% CPU each (bursty)
- Load average: 3.17 on 12 threads (i7-9750H)
- Zero swap usage, 38GB available of 64GB — memory was not the bottleneck

## Fix (3 changes)

### 1. Disabled desktop notifications, webhook only (`config.py`)

```python
# Before:
notification_channels: list[str] = field(default_factory=lambda: ["desktop"])
notification_webhook_url: Optional[str] = None

# After:
notification_channels: list[str] = field(default_factory=lambda: ["webhook"])
notification_webhook_url: Optional[str] = "https://n8n.ethospower.org/webhook/iwo-audit"
```

Desktop `notify-send` calls completely eliminated. All notifications now route through n8n → ntfy → phone.

### 2. Rate-limiting on `_notify()` (`daemon.py`)

Added `_notify_cooldowns` dict and 120-second deduplication for non-critical messages. Messages are keyed on their first 50 characters — the canary warning "⚠️ planner canary failing for 180s" and "⚠️ planner canary failing for 182s" collapse to the same key. Critical messages always pass through immediately.

### 3. Separated queue drain from state poll (`daemon.py` + `tui.py`)

`_process_pending_activations()` (which runs canary probes) was called at the end of every 2s `_poll_agent_states()` tick. Canary probes take up to 10s each, causing probe-on-probe stacking in the async Textual timer.

Fix: In TUI mode, `_poll_agent_states()` skips the `_process_pending_activations()` call. A new `_drain_queues()` method runs on its own 15-second `set_interval` timer, giving each canary probe time to complete before the next cycle. Headless mode (`run_loop()`) is unchanged — its sequential loop already prevents stacking.

## Verification

After restarting IWO:
- `notify-send` should never be called (grep IWO's process tree for notify-send)
- gnome-shell CPU should drop from ~23% to <5% idle
- Webhook notifications should arrive on phone via ntfy (test by queuing a handoff)
- TUI dashboard continues to update agent states every 2s (display unaffected)

## Lessons

- Desktop notifications via `notify-send` are expensive at high frequency — each one triggers compositor recomposition in GNOME Shell
- Retry loops that call `_notify()` must always have deduplication/cooldown
- Long-running operations (canary probes) should never piggyback on high-frequency timers
