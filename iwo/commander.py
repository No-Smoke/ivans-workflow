"""IWO Commander — tmux interaction layer using libtmux.

Phase 1.0: Agent discovery via @iwo-agent pane tags (survives rearrangement).
Canary probe with wait-for-echo. Pipe-pane archival logging.
PS1 setup where possible. Cursor position + output observation.
"""

import logging
import re
import time
from pathlib import Path
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

    def send_canary_and_wait(self, canary: str, timeout: float, poll_interval: float,
                              idle_pattern: str = r"[❯>]\s*$") -> bool:
        """Canary probe for Claude Code agents.

        Strategy: capture a baseline output hash, send a single Enter keystroke,
        wait for the output to change (Claude Code redraws the prompt), then
        confirm the idle prompt reappears. This proves the agent is alive and
        responsive without injecting text that Claude Code would interpret as
        a chat message.

        Falls back to simple idle-prompt check if output doesn't change
        (some terminals don't redraw on bare Enter).
        """
        import re
        pattern = re.compile(idle_pattern)

        # Baseline: confirm prompt is visible now
        baseline_lines = self.capture_visible(10)
        has_prompt = any(pattern.search(l.rstrip()) for l in baseline_lines if l.rstrip())
        if not has_prompt:
            log.warning(f"Canary: {self.agent_name} has no prompt visible — skipping")
            return False

        baseline_hash = hash(tuple(baseline_lines))

        # Send bare Enter (no text — just a keystroke)
        try:
            self.pane.send_keys("", enter=True)
        except Exception as e:
            log.error(f"Canary: failed to send Enter to {self.agent_name}: {e}")
            return False

        # Wait for output to change (prompt redrawn) or timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            lines = self.capture_visible(10)
            current_hash = hash(tuple(lines))

            # Output changed — check if prompt returned
            if current_hash != baseline_hash:
                if any(pattern.search(l.rstrip()) for l in lines if l.rstrip()):
                    log.info(f"Canary confirmed for {self.agent_name} (prompt reappeared)")
                    return True

        # Timeout — but if prompt is still there, agent is likely fine
        # (some terminals don't redraw on bare Enter)
        final_lines = self.capture_visible(10)
        if any(pattern.search(l.rstrip()) for l in final_lines if l.rstrip()):
            log.info(f"Canary: {self.agent_name} prompt still visible (accepting as responsive)")
            return True

        log.warning(f"Canary timeout for {self.agent_name} after {timeout}s — no prompt found")
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
        """Discover agent panes — Phase 1: by @iwo-agent tag, fallback to window index.

        Tag-based discovery survives pane rearrangement. If no tags found,
        falls back to window-index mapping and sets tags for next time.
        """
        self.agents.clear()

        # Try tag-based discovery first
        tagged = self._discover_by_tag()
        if tagged > 0:
            log.info(f"Tag-based discovery: found {tagged}/{len(self.config.agent_window_map)} agents")
            if tagged == len(self.config.agent_window_map):
                return True
            # Partial tags — fill remaining from window index
            missing = set(self.config.agent_window_map.keys()) - set(self.agents.keys())
            log.info(f"Filling {len(missing)} untagged agents from window index: {missing}")
            self._discover_by_window_index(only=missing)
        else:
            # No tags at all — first run, use window index and set tags
            log.info("No @iwo-agent tags found — initial setup via window index")
            self._discover_by_window_index()

        # Tag any untagged panes for future discovery
        self._tag_discovered_agents()

        found = len(self.agents)
        total = len(self.config.agent_window_map)
        log.info(f"Discovered {found}/{total} agents")
        return found > 0

    def _discover_by_tag(self) -> int:
        """Find panes with @iwo-agent user option set."""
        count = 0
        try:
            # List all panes in session with their @iwo-agent tag
            for window in self.session.windows:
                for pane in window.panes:
                    try:
                        result = pane.cmd(
                            "display-message", "-p",
                            "-t", pane.pane_id,
                            f"#{{" + self.config.pane_tag_key + "}"
                        )
                        if result.stdout and result.stdout[0].strip():
                            agent_name = result.stdout[0].strip()
                            if agent_name in self.config.agent_window_map:
                                self.agents[agent_name] = AgentPane(pane, agent_name)
                                log.info(f"Found {agent_name} via tag (pane {pane.pane_id})")
                                count += 1
                    except Exception:
                        pass  # Pane has no tag or error reading it
        except Exception as e:
            log.warning(f"Tag discovery error: {e}")
        return count

    def _discover_by_window_index(self, only: Optional[set[str]] = None):
        """Discover agents by window index (Phase 0.5 fallback)."""
        targets = only or set(self.config.agent_window_map.keys())
        for agent_name in targets:
            window_idx = self.config.agent_window_map[agent_name]
            try:
                windows = self.session.windows
                if window_idx < len(windows):
                    window = windows[window_idx]
                    pane = window.active_pane
                    if pane:
                        self.agents[agent_name] = AgentPane(pane, agent_name)
                        log.info(f"Found {agent_name} at window {window_idx} (pane {pane.pane_id})")
                    else:
                        log.warning(f"No active pane in window {window_idx} for {agent_name}")
                else:
                    log.warning(f"Window {window_idx} not found for {agent_name}")
            except Exception as e:
                log.warning(f"Failed to discover {agent_name}: {e}")

    def _tag_discovered_agents(self):
        """Set @iwo-agent pane user option on all discovered agents."""
        for agent_name, agent_pane in self.agents.items():
            try:
                agent_pane.pane.cmd(
                    "set-option", "-p",
                    "-t", agent_pane.pane_id,
                    self.config.pane_tag_key, agent_name
                )
                log.info(f"Tagged pane {agent_pane.pane_id} as {agent_name}")
            except Exception as e:
                log.warning(f"Failed to tag {agent_name}: {e}")

    def setup_agent_environments(self):
        """Set up pipe-pane archival for all agents."""
        log_dir = str(self.config.log_dir)
        for agent_name, agent_pane in self.agents.items():
            if self.config.enable_pipe_pane:
                agent_pane.setup_pipe_pane(log_dir)

    def activate_agent(self, agent_name: str) -> bool:
        """Send /workflow-next to the specified agent.

        Includes canary probe: sends bare Enter and confirms prompt reappears,
        proving the agent is alive and responsive before injecting the command.
        """
        agent = self.agents.get(agent_name)
        if not agent:
            log.error(f"Agent '{agent_name}' not found in discovered panes")
            return False

        if not agent.is_alive():
            log.error(f"Agent '{agent_name}' pane appears dead")
            return False

        # Canary probe: bare Enter + check prompt returns
        if not agent.send_canary_and_wait(
            self.config.canary_string,
            self.config.canary_timeout_seconds,
            self.config.canary_poll_interval_seconds,
            self.config.idle_prompt_pattern,
        ):
            log.error(f"Agent '{agent_name}' failed canary probe — not sending command")
            return False

        # Brief settle after canary
        time.sleep(1.0)

        return agent.send_command("/workflow-next")

    def get_agent(self, name: str) -> Optional[AgentPane]:
        return self.agents.get(name)

    def launch_agent_007(self, prompt_file: Path) -> bool:
        """Launch Agent 007 in window 6 by piping a prompt file into claude -p.

        Window 6 starts as an idle bash shell. This method pipes the activation
        prompt into claude, which runs, reports, exits, returning the pane to idle.

        Returns True if the command was sent successfully.
        """
        if not self.session:
            log.error("Agent 007: no tmux session connected")
            return False

        windows = self.session.windows
        window_idx = self.config.agent_007_window
        if window_idx >= len(windows):
            log.error(f"Agent 007: window {window_idx} does not exist")
            return False

        window = windows[window_idx]
        pane = window.active_pane
        if not pane:
            log.error("Agent 007: no active pane in window 6")
            return False

        # Verify pane is at a bash prompt (not already running claude)
        if not self.check_agent_007_idle():
            log.warning("Agent 007: pane is not idle — already active or not at bash prompt")
            return False

        # Build launch command — pipe from file to avoid shell escaping issues
        budget = self.config.agent_007_budget_usd
        cmd = (
            f"cat {prompt_file} | claude -p "
            f"--dangerously-skip-permissions "
            f"--no-session-persistence "
            f"--max-budget-usd {budget}"
        )

        try:
            pane.send_keys(cmd, enter=True)
            log.info(f"Agent 007: launched with prompt from {prompt_file}")
            return True
        except Exception as e:
            log.error(f"Agent 007: failed to launch: {e}")
            return False

    def check_agent_007_idle(self) -> bool:
        """Check if Agent 007's pane (window 6) is at a bash prompt.

        Returns True if the pane shows a bash prompt and no Claude Code indicators,
        meaning 007 has exited (or was never launched).
        """
        if not self.session:
            return False

        windows = self.session.windows
        window_idx = self.config.agent_007_window
        if window_idx >= len(windows):
            return False

        window = windows[window_idx]
        pane = window.active_pane
        if not pane:
            return False

        try:
            lines = pane.cmd("capture-pane", "-p", "-J").stdout
            last_lines = lines[-10:] if len(lines) > 10 else lines
            text = "\n".join(last_lines)

            # Claude Code indicators — if present, 007 is still running
            claude_patterns = re.compile(r"(❯|Claude Code|claude-code|╭─|Tips for|/help|Opus|Sonnet)")
            if claude_patterns.search(text):
                return False

            # Check for bash prompt at end of output
            bash_prompt = re.compile(r"[$#]\s*$", re.MULTILINE)
            if bash_prompt.search(text):
                return True

            # No recognizable prompt — ambiguous, assume not idle
            return False
        except Exception as e:
            log.warning(f"Agent 007: idle check failed: {e}")
            return False

    def respawn_agent(self, agent_name: str) -> bool:
        """Respawn a crashed agent pane and re-launch Claude Code.

        Steps:
        1. tmux respawn-pane to get a fresh shell
        2. cd to project root and launch 'claude'
        3. Wait for Claude Code prompt (up to 60s)
        4. Re-tag the pane for IWO discovery

        Returns True if the pane was respawned and Claude Code started.
        Does NOT send the agent's skill initialization — that happens
        separately when the agent becomes IDLE and gets activated.
        """
        agent = self.agents.get(agent_name)
        if not agent:
            log.error(f"Respawn: agent '{agent_name}' not found")
            return False

        pane = agent.pane

        # 1. Respawn the pane (kills old process, starts fresh shell)
        try:
            pane.cmd("respawn-pane", "-k", "-t", pane.pane_id)
            log.info(f"Respawn: pane respawned for {agent_name}")
        except Exception as e:
            log.error(f"Respawn: respawn-pane failed for {agent_name}: {e}")
            return False

        time.sleep(2)  # Let shell initialize

        # 2. cd to project and launch Claude Code
        project_dir = str(self.config.project_root)
        try:
            pane.send_keys(f"cd {project_dir} && claude", enter=True)
            log.info(f"Respawn: launched Claude Code for {agent_name}")
        except Exception as e:
            log.error(f"Respawn: failed to launch Claude Code for {agent_name}: {e}")
            return False

        # 3. Wait for Claude Code prompt (poll for up to 60s)
        import re
        prompt_pattern = re.compile(r"(❯|Claude Code|claude-code|╭─|Tips for|/help|Opus|Sonnet)")
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(3)
            try:
                lines = pane.cmd("capture-pane", "-p", "-J").stdout
                text = "\n".join(lines[-30:]) if lines else ""
                if prompt_pattern.search(text):
                    log.info(f"Respawn: Claude Code ready for {agent_name}")
                    break
            except Exception:
                pass
        else:
            log.warning(f"Respawn: Claude Code not detected for {agent_name} after 60s")
            return False

        # 4. Re-tag the pane
        try:
            pane.cmd(
                "set-option", "-p",
                "-t", pane.pane_id,
                self.config.pane_tag_key, agent_name
            )
        except Exception:
            pass  # Non-fatal

        # 5. Re-enable pipe-pane archival
        if self.config.enable_pipe_pane:
            agent.setup_pipe_pane(str(self.config.log_dir))

        log.info(f"Respawn: {agent_name} successfully respawned")
        return True
