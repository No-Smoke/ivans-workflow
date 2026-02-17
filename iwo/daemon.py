"""IWO Daemon — Phase 0.5 'Smart Relay'.

Watches for handoff files, validates them, routes to next agent.
Uses libtmux for reliable tmux interaction.
No state machine yet — that's Phase 1.
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

        # Track per-spec counts
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

        # Only process .json files, skip .tmp and LATEST.json
        if path.suffix != ".json":
            return
        if path.name == "LATEST.json":
            return
        if path.name.endswith(".tmp"):
            return

        log.info(f"New handoff detected: {path.name}")

        # Debounce: wait for file write to complete
        time.sleep(self.daemon.config.file_debounce_seconds)

        self.daemon.process_handoff(path)


class IWODaemon:
    """Main orchestrator daemon."""

    def __init__(self, config: Optional[IWOConfig] = None):
        self.config = config or IWOConfig()
        self.commander = TmuxCommander(self.config)
        self.tracker = HandoffTracker()
        self.observer: Optional[Observer] = None

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
            f"{handoff.source_agent} -> {handoff.target_agent} "
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

        # 5. Update LATEST.json symlink (orchestrator owns this)
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
            return  # Do NOT auto-activate deployer

        # 7. Activate next agent
        log.info(f"Activating {target}...")
        time.sleep(2)  # Brief pause to let agent settle
        success = self.commander.activate_agent(target)
        if success:
            self._notify(f"✅ Activated {target} for {handoff.spec_id} (#{handoff.sequence})")
        else:
            self._notify(f"❌ Failed to activate {target}", critical=True)

    def _update_latest(self, handoff_path: Path, handoff: Handoff):
        """Update LATEST.json as a symlink to the current handoff."""
        spec_dir = handoff_path.parent
        latest = spec_dir / "LATEST.json"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(handoff_path.name)
            log.info(f"LATEST.json -> {handoff_path.name}")
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

    def start(self):
        """Start the daemon: connect to tmux, begin watching for handoffs."""
        log.info("=" * 60)
        log.info("IWO — Ivan's Workflow Orchestrator v0.5")
        log.info("=" * 60)

        # 1. Connect to tmux
        if not self.commander.connect():
            log.error("Cannot connect to tmux session. Is the workflow running?")
            sys.exit(1)

        # 2. Scan for current state (stateless recovery)
        self._recover_state()

        # 3. Start filesystem watcher
        handler = HandoffHandler(self)
        self.observer = Observer()

        # Watch all spec directories (recursive)
        watch_path = str(self.config.handoffs_dir)
        self.observer.schedule(handler, watch_path, recursive=True)
        self.observer.start()
        log.info(f"Watching: {watch_path}")
        self._notify("IWO started — watching for handoffs")

        # 4. Main loop
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.observer.stop()
        self.observer.join()
        log.info("IWO stopped.")

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

        # Find highest-sequence handoff
        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json"]
        if not json_files:
            log.info(f"No handoffs found for {spec_id}")
            return

        latest = json_files[-1]
        log.info(f"Recovery: Current spec={spec_id}, latest handoff={latest.name}")

        # Mark all existing handoffs as processed to avoid re-triggering
        for f in json_files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                handoff = Handoff(**data)
                self.tracker.mark_processed(handoff)
            except Exception:
                pass  # Skip unparseable files during recovery

        log.info(f"Recovery: Marked {len(json_files)} existing handoffs as processed")


def main():
    """Entry point."""
    config = IWOConfig()

    # Allow overriding project root via env var
    if root := os.environ.get("IWO_PROJECT_ROOT"):
        config.project_root = Path(root)
        config.handoffs_dir = config.project_root / "docs" / "agent-comms"

    daemon = IWODaemon(config)
    daemon.start()


if __name__ == "__main__":
    main()
