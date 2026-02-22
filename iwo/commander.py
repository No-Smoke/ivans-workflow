"""IWO Commander — tmux pane abstraction layer using libtmux.

AgentPane wraps a libtmux pane tagged as an IWO agent.
Used by HeadlessCommander for pane discovery, pipe-pane archival,
and cursor/output observation.

Note: TmuxCommander (canary-probe dispatch) was removed in Phase 3.
All agent dispatch now goes through HeadlessCommander (headless_commander.py).
"""

import logging
import time
from typing import Optional

import libtmux

from .config import IWOConfig

log = logging.getLogger("iwo.commander")


class AgentPane:
    """Wrapper around a libtmux pane tagged as an IWO agent."""

    def __init__(self, pane: libtmux.Pane, agent_name: str):
        self.pane = pane
        self.agent_name = agent_name
        self.pane_id = pane.pane_id
        self._last_command_time: float = 0.0

    def capture_visible(self, last_n_lines: int = 30) -> list[str]:
        """Capture the last N visible lines from the pane."""
        try:
            lines = self.pane.cmd("capture-pane", "-p", "-J").stdout
            return lines[-last_n_lines:] if len(lines) > last_n_lines else lines
        except Exception as e:
            log.error(f"capture-pane failed for {self.agent_name} ({self.pane_id}): {e}")
            return []

    def get_cursor_position(self) -> Optional[tuple[int, int]]:
        """Get cursor (x, y) position — Gemini 3 Pro's phantom-prompt fix."""
        try:
            result = self.pane.cmd(
                "display-message", "-p",
                "-t", self.pane_id,
                "#{cursor_x},#{cursor_y}"
            )
            if result.stdout:
                parts = result.stdout[0].split(",")
                return (int(parts[0]), int(parts[1]))
        except Exception as e:
            log.warning(f"cursor position check failed for {self.agent_name}: {e}")
        return None

    def send_command(self, command: str) -> bool:
        """Send a command to the agent pane via send-keys.

        Returns True if command was sent successfully.
        """
        try:
            self.pane.send_keys(command, enter=True, suppress_history=False)
            self._last_command_time = time.time()
            log.info(f"Sent to {self.agent_name} ({self.pane_id}): {command[:80]}")
            return True
        except Exception as e:
            log.error(f"Failed to send to {self.agent_name} ({self.pane_id}): {e}")
            return False

    def is_alive(self) -> bool:
        """Check if the pane process is still running."""
        try:
            cmd = self.pane.pane_current_command
            return cmd is not None and cmd != ""
        except Exception:
            return False

    def setup_pipe_pane(self, log_dir: str) -> bool:
        """Enable pipe-pane to archive all output to a log file."""
        log_path = f"{log_dir}/agent-{self.agent_name}.log"
        try:
            self.pane.cmd(
                "pipe-pane", "-o",
                "-t", self.pane_id,
                f"cat >> {log_path}"
            )
            log.info(f"pipe-pane archival enabled: {self.agent_name} → {log_path}")
            return True
        except Exception as e:
            log.warning(f"pipe-pane setup failed for {self.agent_name}: {e}")
            return False
