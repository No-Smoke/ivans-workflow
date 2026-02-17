"""IWO Commander — tmux interaction layer using libtmux.

Handles agent discovery via @iwo-agent pane tags, command injection
via paste-buffer, and output observation via capture-pane.
"""

import logging
import re
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
        self._last_output_hash: Optional[int] = None
        self._last_output_time: float = 0.0
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
        """Send a command to the agent pane via paste-buffer (GPT-5.2's recommendation).

        Returns True if command was sent successfully.
        """
        try:
            self.pane.send_keys(command, enter=True, suppress_history=False)
            self._last_command_time = time.time()
            log.info(f"Sent command to {self.agent_name} ({self.pane_id}): {command[:60]}...")
            return True
        except Exception as e:
            log.error(f"Failed to send command to {self.agent_name} ({self.pane_id}): {e}")
            return False

    def send_canary(self, canary: str) -> bool:
        """Send a canary probe to check if the shell is responsive."""
        return self.send_command(f"echo '{canary}'")

    def check_output_changed(self) -> bool:
        """Check if pane output has changed since last check."""
        lines = self.capture_visible(30)
        current_hash = hash(tuple(lines))
        changed = current_hash != self._last_output_hash
        self._last_output_hash = current_hash
        if changed:
            self._last_output_time = time.time()
        return changed

    def is_alive(self) -> bool:
        """Check if the pane process is still running."""
        try:
            cmd = self.pane.pane_current_command
            return cmd is not None and cmd != ""
        except Exception:
            return False


class TmuxCommander:
    """Manages tmux session and agent pane discovery/interaction."""

    def __init__(self, config: IWOConfig):
        self.config = config
        self.server: Optional[libtmux.Server] = None
        self.session: Optional[libtmux.Session] = None
        self.agents: dict[str, AgentPane] = {}

    def connect(self) -> bool:
        """Connect to the tmux server and discover agent panes."""
        try:
            self.server = libtmux.Server()
            sessions = self.server.sessions.filter(session_name=self.config.tmux_session_name)
            if not sessions:
                log.error(f"tmux session '{self.config.tmux_session_name}' not found")
                return False
            self.session = sessions[0]
            log.info(f"Connected to tmux session: {self.config.tmux_session_name}")
            return self._discover_agents()
        except Exception as e:
            log.error(f"Failed to connect to tmux: {e}")
            return False

    def _discover_agents(self) -> bool:
        """Discover agent panes by window index mapping.

        Phase 0.5: Uses window index. Phase 1 will use @iwo-agent tags.
        """
        self.agents.clear()
        for agent_name, window_idx in self.config.agent_window_map.items():
            try:
                windows = self.session.windows
                if window_idx < len(windows):
                    window = windows[window_idx]
                    pane = window.active_pane
                    if pane:
                        self.agents[agent_name] = AgentPane(pane, agent_name)
                        log.info(f"Discovered {agent_name} at window {window_idx} (pane {pane.pane_id})")
                    else:
                        log.warning(f"No active pane in window {window_idx} for {agent_name}")
                else:
                    log.warning(f"Window {window_idx} not found for {agent_name}")
            except Exception as e:
                log.warning(f"Failed to discover {agent_name}: {e}")

        found = len(self.agents)
        total = len(self.config.agent_window_map)
        log.info(f"Discovered {found}/{total} agents")
        return found > 0

    def activate_agent(self, agent_name: str) -> bool:
        """Send /workflow-next to the specified agent."""
        agent = self.agents.get(agent_name)
        if not agent:
            log.error(f"Agent '{agent_name}' not found in discovered panes")
            return False

        if not agent.is_alive():
            log.error(f"Agent '{agent_name}' pane appears dead")
            return False

        return agent.send_command("/workflow-next")

    def get_agent(self, name: str) -> Optional[AgentPane]:
        return self.agents.get(name)
