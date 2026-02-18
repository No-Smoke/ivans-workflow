"""IWO Daemon — Phase 1.0 'State Machine'.

Watches for handoff files, validates them, checks agent state via
state machine, sends canary probe, and routes to next agent.

Phase 1 additions over 0.5:
- Agent state machine (IDLE/PROCESSING/STUCK/WAITING_HUMAN/CRASHED)
- Canary probe before command injection
- Pre-activation state validation
- 30-second filesystem reconciliation
- Pipe-pane archival logging
- Pane tag-based discovery

Design: Three-model consensus (Claude Opus 4.6 + GPT-5.2 + Gemini 3 Pro).
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from .config import IWOConfig
from .parser import Handoff
from .commander import TmuxCommander
from .state import AgentState, AgentStateMachine
from .memory import IWOMemory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iwo.daemon")


class HandoffTracker:
    """Tracks processed handoffs to prevent duplicates (GPT-5.2's idempotency key)."""

    def __init__(self):
        self._processed: set[str] = set()
        self._spec_handoff_counts: dict[str, int] = {}
        self._rejection_counts: dict[str, int] = {}  # key: "spec:agent_pair"

    def already_processed(self, handoff: Handoff) -> bool:
        return handoff.idempotency_key in self._processed

    def mark_processed(self, handoff: Handoff):
        self._processed.add(handoff.idempotency_key)
        spec = handoff.spec_id
        self._spec_handoff_counts[spec] = self._spec_handoff_counts.get(spec, 0) + 1

    def check_rejection_loop(self, handoff: Handoff, max_loops: int) -> bool:
        """Returns True if rejection loop threshold exceeded."""
        if not handoff.is_rejection:
            return False
        key = f"{handoff.spec_id}:{handoff.source_agent}->{handoff.target_agent}"
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        count = self._rejection_counts[key]
        if count >= max_loops:
            log.warning(f"Rejection loop threshold ({max_loops}) hit: {key} ({count} times)")
            return True
        return False

    def check_handoff_limit(self, handoff: Handoff, max_handoffs: int) -> bool:
        """Returns True if handoff count limit exceeded for this spec."""
        count = self._spec_handoff_counts.get(handoff.spec_id, 0)
        return count >= max_handoffs


class HandoffHandler(FileSystemEventHandler):
    """Watchdog handler for new handoff JSON files."""

    def __init__(self, daemon: "IWODaemon"):
        self.daemon = daemon

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".json":
            return
        if path.name == "LATEST.json":
            return
        if path.name.endswith(".tmp"):
            return
        log.info(f"New handoff detected: {path.name}")
        time.sleep(self.daemon.config.file_debounce_seconds)
        self.daemon.process_handoff(path)


class IWODaemon:
    """Main orchestrator daemon — Phase 1.0."""

    def __init__(self, config: Optional[IWOConfig] = None):
        self.config = config or IWOConfig()
        self.commander = TmuxCommander(self.config)
        self.tracker = HandoffTracker()
        self.observer: Optional[Observer] = None

        # Phase 1: state machines per agent
        self.state_machines: dict[str, AgentStateMachine] = {}

        # Pending activations: handoffs waiting for target agent to become IDLE
        self._pending_activations: list[tuple[Handoff, Path]] = []

        # Phase 2: handoff history for dashboard display (most recent first)
        self.handoff_history: list[Handoff] = []
        self._max_history: int = 50

        # Startup timestamp
        self._started_at: float = time.time()

        # Phase 2.1: Memory integration
        self.memory: Optional[IWOMemory] = None

    def _init_state_machines(self):
        """Create state machines for all discovered agents."""
        self.state_machines.clear()
        for name, agent_pane in self.commander.agents.items():
            self.state_machines[name] = AgentStateMachine(agent_pane, self.config)
            log.info(f"State machine initialized for {name}")

    def _poll_agent_states(self):
        """Poll all agent state machines. Called every ~2s from main loop."""
        for name, sm in self.state_machines.items():
            prev = sm.state
            current = sm.poll()
            # Log only transitions (poll() already logs them, but we handle side effects here)
            if current == AgentState.WAITING_HUMAN and prev != AgentState.WAITING_HUMAN:
                self._notify(
                    f"🙋 {name} needs human input — check tmux",
                    critical=True,
                )
            elif current == AgentState.STUCK and prev != AgentState.STUCK:
                self._notify(
                    f"⏳ {name} appears stuck (no output for {self.config.stuck_timeout_seconds}s)",
                    critical=True,
                )
            elif current == AgentState.CRASHED and prev != AgentState.CRASHED:
                self._notify(
                    f"💀 {name} has crashed — pane process exited",
                    critical=True,
                )

        # Check if any pending activations can proceed
        self._process_pending_activations()

    def _process_pending_activations(self):
        """Try to activate agents for queued handoffs whose target is now IDLE."""
        if not self._pending_activations:
            return

        still_pending = []
        for handoff, path in self._pending_activations:
            target = handoff.target_agent
            sm = self.state_machines.get(target)
            if sm and sm.state == AgentState.IDLE:
                log.info(f"Pending activation: {target} is now IDLE, activating...")
                success = self.commander.activate_agent(target)
                if success:
                    sm.mark_command_sent()
                    self._notify(f"✅ Activated {target} for {handoff.spec_id} (#{handoff.sequence})")
                else:
                    self._notify(f"❌ Failed to activate {target}", critical=True)
                    still_pending.append((handoff, path))  # Retry later
            else:
                state_str = sm.state.value if sm else "unknown"
                still_pending.append((handoff, path))

        if len(still_pending) != len(self._pending_activations):
            log.info(f"Pending activations: {len(still_pending)} remaining")
        self._pending_activations = still_pending

    def process_handoff(self, path: Path):
        """Parse, validate, and route a handoff file."""
        # 1. Parse and validate
        try:
            with open(path) as f:
                data = json.load(f)
            handoff = Handoff(**data)
        except json.JSONDecodeError as e:
            log.error(f"Invalid JSON in {path.name}: {e}")
            self._notify(f"Invalid JSON: {path.name}", critical=True)
            return
        except ValidationError as e:
            log.error(f"Handoff validation failed for {path.name}: {e}")
            self._notify(f"Invalid handoff structure: {path.name}", critical=True)
            return

        log.info(
            f"Handoff #{handoff.sequence}: "
            f"{handoff.source_agent} → {handoff.target_agent} "
            f"[{handoff.status.outcome}] ({handoff.spec_id})"
        )

        # 2. Idempotency check
        if self.tracker.already_processed(handoff):
            log.info(f"Already processed {handoff.idempotency_key}, skipping")
            return

        # 3. Safety rails
        if self.tracker.check_handoff_limit(handoff, self.config.max_handoffs_per_spec):
            msg = f"HALT: {handoff.spec_id} exceeded {self.config.max_handoffs_per_spec} handoffs"
            log.error(msg)
            self._notify(msg, critical=True)
            return

        if self.tracker.check_rejection_loop(handoff, self.config.max_rejection_loops):
            msg = f"HALT: Rejection loop detected in {handoff.spec_id}"
            log.error(msg)
            self._notify(msg, critical=True)
            return

        # 4. Mark processed
        self.tracker.mark_processed(handoff)
        self.handoff_history.insert(0, handoff)
        if len(self.handoff_history) > self._max_history:
            self.handoff_history.pop()

        # 4.1 Store to memory (best-effort, non-blocking)
        if self.memory:
            try:
                proc_start = time.monotonic()
                self.memory.store_handoff(handoff, processing_time_ms=0)
                proc_ms = (time.monotonic() - proc_start) * 1000
                log.info(f"Memory: stored handoff in {proc_ms:.0f}ms")
            except Exception as e:
                log.warning(f"Memory: store failed (non-fatal): {e}")

        # 5. Update LATEST.json symlink
        self._update_latest(path, handoff)

        # 6. Human gate check
        target = handoff.target_agent
        if target in self.config.human_gate_agents:
            msg = (
                f"🚦 DEPLOY GATE: {handoff.spec_id} ready for {target}. "
                f"Action: {handoff.nextAgent.action[:100]}"
            )
            log.info(msg)
            self._notify(msg, critical=True)
            return

        # 7. Phase 1: Check target agent state before activating
        sm = self.state_machines.get(target)
        if sm:
            if sm.state == AgentState.IDLE:
                # Agent is idle — activate immediately
                log.info(f"Target {target} is IDLE — activating now")
                time.sleep(1)  # Brief settle
                success = self.commander.activate_agent(target)
                if success:
                    sm.mark_command_sent()
                    self._notify(f"✅ Activated {target} for {handoff.spec_id} (#{handoff.sequence})")
                else:
                    self._notify(f"❌ Failed to activate {target}", critical=True)
            elif sm.state in (AgentState.PROCESSING, AgentState.UNKNOWN):
                # Agent is busy or state unknown — queue for later
                log.info(f"Target {target} is {sm.state.value} — queuing activation")
                self._pending_activations.append((handoff, path))
                self._notify(
                    f"⏸️ Queued {target} activation for {handoff.spec_id} "
                    f"(agent is {sm.state.value})"
                )
            elif sm.state in (AgentState.STUCK, AgentState.WAITING_HUMAN, AgentState.CRASHED):
                # Agent is in a bad state — notify and queue
                msg = (
                    f"⚠️ Target {target} is {sm.state.value} — "
                    f"cannot activate for {handoff.spec_id}. Queued."
                )
                log.warning(msg)
                self._notify(msg, critical=True)
                self._pending_activations.append((handoff, path))
        else:
            # No state machine (shouldn't happen) — fall back to direct activation
            log.warning(f"No state machine for {target}, activating directly")
            time.sleep(2)
            success = self.commander.activate_agent(target)
            if success:
                self._notify(f"✅ Activated {target} for {handoff.spec_id} (#{handoff.sequence})")
            else:
                self._notify(f"❌ Failed to activate {target}", critical=True)

    def _reconcile_filesystem(self):
        """Periodic scan to catch missed inotify events. Called every 30s."""
        current_spec_file = self.config.handoffs_dir / ".current-spec"
        if not current_spec_file.exists():
            return

        try:
            spec_id = current_spec_file.read_text().strip()
        except Exception:
            return

        spec_dir = self.config.handoffs_dir / spec_id
        if not spec_dir.exists():
            return

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json" and not f.name.endswith(".tmp")]

        missed = 0
        for f in json_files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                handoff = Handoff(**data)
                if not self.tracker.already_processed(handoff):
                    log.info(f"Reconciliation: found missed handoff {f.name}")
                    self.process_handoff(f)
                    missed += 1
            except Exception:
                pass

        if missed > 0:
            log.info(f"Reconciliation: processed {missed} missed handoff(s)")

    def _update_latest(self, handoff_path: Path, handoff: Handoff):
        """Update LATEST.json as a symlink to the current handoff."""
        spec_dir = handoff_path.parent
        latest = spec_dir / "LATEST.json"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(handoff_path.name)
            log.info(f"LATEST.json → {handoff_path.name}")
        except Exception as e:
            log.warning(f"Failed to update LATEST.json: {e}")

    def _notify(self, message: str, critical: bool = False):
        """Send desktop notification via notify-send."""
        urgency = "critical" if critical else "normal"
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "IWO", "Ivan's Workflow", message],
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            log.warning(f"notify-send failed: {e}")

    def setup(self) -> bool:
        """Initialize daemon: connect to tmux, set up state machines, recover state, start watcher.

        Returns True if setup succeeded. Called by both headless start() and TUI mode.
        Does NOT enter the main loop — call run_loop() for headless or let TUI drive polling.
        """
        log.info("=" * 60)
        log.info("IWO — Ivan's Workflow Orchestrator v1.0")
        log.info("Phase 1: State Machine + Canary Probes + Tag Discovery")
        log.info("=" * 60)

        # 1. Connect to tmux and discover agents (with tag-based discovery)
        if not self.commander.connect():
            log.error("Cannot connect to tmux session. Is the workflow running?")
            return False

        # 2. Set up agent environments (pipe-pane archival)
        self.commander.setup_agent_environments()

        # 3. Initialize state machines for all discovered agents
        self._init_state_machines()

        # 4. Scan for current state (stateless recovery)
        self._recover_state()

        # 5. Start filesystem watcher
        handler = HandoffHandler(self)
        self.observer = Observer()
        watch_path = str(self.config.handoffs_dir)
        self.observer.schedule(handler, watch_path, recursive=True)
        self.observer.start()
        log.info(f"Watching: {watch_path}")

        # 6. Initialize memory integration (best-effort)
        if self.config.enable_memory:
            self.memory = IWOMemory(self.config)
            if self.memory.initialize():
                log.info("Memory integration active")
            else:
                log.warning("Memory integration unavailable — continuing without persistence")

        self._notify("IWO v1.0 started — state machine active")
        return True

    def run_loop(self):
        """Headless main loop: state polling + reconciliation on tick counters.

        For TUI mode, the Textual app drives polling via set_interval instead.
        """
        poll_every = max(1, int(self.config.state_poll_interval_seconds))
        recon_every = self.config.reconciliation_interval_seconds
        tick = 0

        try:
            while True:
                time.sleep(1)
                tick += 1

                if tick % poll_every == 0:
                    self._poll_agent_states()

                if tick % recon_every == 0:
                    self._reconcile_filesystem()

        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.observer.stop()
        self.observer.join()
        log.info("IWO stopped.")

    def shutdown(self):
        """Clean shutdown — stop observer, close memory, and log."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.memory:
            self.memory.close()
        log.info("IWO stopped.")

    def start(self):
        """Start the daemon: connect to tmux, init state machines, watch for handoffs."""
        if not self.setup():
            sys.exit(1)
        self.run_loop()

    def _recover_state(self):
        """Scan filesystem to reconstruct state after restart."""
        current_spec_file = self.config.handoffs_dir / ".current-spec"
        if not current_spec_file.exists():
            log.info("No active spec found")
            return

        spec_id = current_spec_file.read_text().strip()
        spec_dir = self.config.handoffs_dir / spec_id
        if not spec_dir.exists():
            log.warning(f"Spec dir not found: {spec_dir}")
            return

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json"]
        if not json_files:
            log.info(f"No handoffs found for {spec_id}")
            return

        latest = json_files[-1]
        log.info(f"Recovery: spec={spec_id}, latest={latest.name}")

        for f in json_files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                handoff = Handoff(**data)
                self.tracker.mark_processed(handoff)
            except Exception:
                pass

        log.info(f"Recovery: marked {len(json_files)} existing handoffs as processed")


def main():
    """Entry point."""
    config = IWOConfig()
    if root := os.environ.get("IWO_PROJECT_ROOT"):
        config.project_root = Path(root)
        config.handoffs_dir = config.project_root / "docs" / "agent-comms"

    daemon = IWODaemon(config)
    daemon.start()


if __name__ == "__main__":
    main()
