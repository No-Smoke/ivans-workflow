"""HeadlessCommander — Dual-mode agent dispatch.

Supports two dispatch modes depending on pane state:

1. **Headless** (pane at bash prompt):
    cd $PROJECT && cat prompt.md | claude -p \
        --output-format stream-json \
        --permission-mode bypassPermissions \
        --append-system-prompt-file .claude/skills/$SKILL/SKILL.md \
        2>&1 | tee $LOG

2. **Interactive** (pane has Claude Code session at `>` prompt):
    Sends `/workflow-next` to the existing interactive session.

Idle detection is dual-mode:
  - pane_current_command ∈ IDLE_SHELLS → bash idle (headless dispatch)
  - pane_current_command == "claude" + `>` prompt visible → interactive idle

Design: Three-model consensus (Claude Opus 4.6, GPT-5.2, Gemini 3 Pro).
"""

import logging
import time
from pathlib import Path
from typing import Optional

import libtmux

from .commander import AgentPane
from .config import IWOConfig

log = logging.getLogger("iwo.headless")

# IWO agent name → eBatt skill directory name
SKILL_DIR_MAP: dict[str, str] = {
    "planner": "boris-planner-agent",
    "builder": "boris-builder-agent",
    "reviewer": "boris-reviewer-agent",
    "tester": "boris-tester-agent",
    "deployer": "boris-deployer-agent",
    "docs": "boris-docs-agent",
}

# Shell names that indicate an idle pane (no claude process running)
IDLE_SHELLS = frozenset(("bash", "zsh", "sh", "fish"))

# Strip CLAUDECODE env var to avoid "nested session" detection when
# claude -p is launched from inside an existing Claude Code session.
# tmux panes inherit the parent env, so we clean it in the command.
CLEAN_ENV_PREFIX = 'unset CLAUDECODE;'


class HeadlessCommander:
    """Manages agent panes and dispatches work via headless claude -p.

    Lifecycle:
        commander = HeadlessCommander(config)
        commander.connect()          # attach to tmux session
        commander.activate_agent(name, handoff, path)  # dispatch work
        completed = commander.check_completions()       # poll for done
    """

    def __init__(self, config: IWOConfig):
        self.config = config
        self._server: Optional[libtmux.Server] = None
        self._session: Optional[libtmux.Session] = None
        self._agents: dict[str, AgentPane] = {}

        # Track which agents currently have a claude -p process running
        self._active_agents: set[str] = set()

        # Session IDs from stream-json output (for potential resumption)
        self._session_ids: dict[str, str] = {}

        # Ensure prompt and log directories exist
        self._prompt_dir = config.log_dir / "prompts"
        self._prompt_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Connection & Discovery
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to tmux server and discover agent panes."""
        try:
            self._server = libtmux.Server()
            self._session = self._server.sessions.filter(
                session_name=self.config.tmux_session_name
            ).get()
            if not self._session:
                log.error(f"tmux session '{self.config.tmux_session_name}' not found")
                return False
        except Exception as e:
            log.error(f"Failed to connect to tmux: {e}")
            return False

        return self._discover_agents()

    def _discover_agents(self) -> bool:
        """Find agent panes by tag, then fall back to window index."""
        found = self._discover_by_tag()
        if found < len(self.config.agent_window_map):
            missing = set(self.config.agent_window_map) - set(self._agents)
            log.info(f"Tag discovery found {found}, falling back for: {missing}")
            self._discover_by_window_index(only=missing)
            self._tag_discovered_agents()

        # Also discover Agent 007 pane
        self._discover_agent_007()

        log.info(
            f"Discovered {len(self._agents)} agents: "
            f"{', '.join(sorted(self._agents.keys()))}"
        )
        return len(self._agents) > 0

    def _discover_by_tag(self) -> int:
        """Find panes tagged with @iwo-agent user option."""
        count = 0
        if not self._session:
            return count
        for window in self._session.windows:
            for pane in window.panes:
                try:
                    tag = pane.pane_current_command  # dummy read to ensure pane is alive
                    # Check user option
                    agent_name = pane.display_message(
                        f"#{{@{self.config.pane_tag_key[1:]}}}", get_option=True
                    ) if hasattr(pane, 'display_message') else None

                    # libtmux approach: use tmux show-options
                    import subprocess
                    result = subprocess.run(
                        ["tmux", "show-options", "-p", "-t", pane.pane_id,
                         "-v", self.config.pane_tag_key],
                        capture_output=True, text=True,
                    )
                    agent_name = result.stdout.strip() if result.returncode == 0 else ""

                    if agent_name and agent_name in self.config.agent_window_map:
                        self._agents[agent_name] = AgentPane(pane, agent_name)
                        count += 1
                except Exception:
                    continue
        return count

    def _discover_by_window_index(self, only: Optional[set[str]] = None):
        """Fall back to window-index mapping for undiscovered agents."""
        if not self._session:
            return
        targets = only or set(self.config.agent_window_map.keys())
        for agent_name in targets:
            if agent_name in self._agents:
                continue
            win_idx = self.config.agent_window_map.get(agent_name)
            if win_idx is None:
                continue
            try:
                windows = self._session.windows.filter(window_index=str(win_idx))
                window = windows.get() if windows else None
                if window and window.panes:
                    pane = window.panes[0]
                    self._agents[agent_name] = AgentPane(pane, agent_name)
            except Exception as e:
                log.warning(f"Window-index discovery failed for {agent_name}: {e}")

    def _tag_discovered_agents(self):
        """Set @iwo-agent pane user option on discovered panes."""
        import subprocess
        for name, agent_pane in self._agents.items():
            try:
                subprocess.run(
                    ["tmux", "set-option", "-p", "-t", agent_pane.pane.pane_id,
                     self.config.pane_tag_key, name],
                    capture_output=True, check=True,
                )
            except Exception as e:
                log.warning(f"Failed to tag pane for {name}: {e}")

    def _discover_agent_007(self):
        """Discover Agent 007 pane by window index."""
        if not self._session:
            return
        try:
            windows = self._session.windows.filter(
                window_index=str(self.config.agent_007_window)
            )
            window = windows.get() if windows else None
            if window and window.panes:
                self._agents["agent-007"] = AgentPane(window.panes[0], "agent-007")
        except Exception as e:
            log.debug(f"Agent 007 pane not found: {e}")

    def setup_agent_environments(self):
        """Set up pipe-pane logging for all discovered agents."""
        if not self.config.enable_pipe_pane:
            return
        for name, agent_pane in self._agents.items():
            agent_pane.setup_pipe_pane(str(self.config.log_dir))

    # ------------------------------------------------------------------
    # Idle Detection (deterministic)
    # ------------------------------------------------------------------

    def is_agent_idle(self, agent_name: str) -> bool:
        """Check if agent pane is idle and ready for dispatch.

        Dual-mode detection:
          1. pane_current_command ∈ IDLE_SHELLS → bash idle (headless mode)
          2. pane_current_command == "claude" AND interactive `>` prompt
             visible AND agent not in _active_agents → interactive idle

        Returns True if the agent can accept new work.
        """
        agent = self._agents.get(agent_name)
        if not agent:
            return False
        try:
            cmd = agent.pane.pane_current_command
            # Mode 1: bash/shell idle — ready for headless claude -p
            if cmd in IDLE_SHELLS:
                return True
            # Mode 2: interactive Claude session at `>` prompt
            if cmd == "claude" and agent_name not in self._active_agents:
                return self._is_interactive_prompt(agent)
            return False
        except Exception:
            return False

    def _is_interactive_prompt(self, agent: AgentPane) -> bool:
        """Check if an interactive Claude Code session is at its `>` prompt.

        Captures the last few visible lines from the pane and looks for
        the Claude Code input prompt pattern: a line starting with `>`.
        This indicates the session is idle and waiting for user input.
        """
        try:
            lines = agent.capture_visible(last_n_lines=5)
            for line in reversed(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                # Claude Code prompt: line is just ">" or starts with "> "
                # Also matches "> /workflow-next" (partially typed command)
                if stripped == ">" or stripped.startswith("> "):
                    return True
                # Any non-empty, non-prompt line means Claude is outputting
                return False
        except Exception as e:
            log.debug(f"Interactive prompt check failed for {agent.agent_name}: {e}")
        return False

    # ------------------------------------------------------------------
    # Agent Dispatch
    # ------------------------------------------------------------------

    def activate_agent(
        self,
        agent_name: str,
        handoff: "Handoff",
        handoff_path: Path,
    ) -> bool:
        """Dispatch work to an agent — dual-mode.

        Detects pane state and routes to the appropriate dispatch mode:
          - bash/shell idle → headless `claude -p` dispatch
          - interactive Claude at `>` prompt → `/workflow-next` dispatch

        Returns True if dispatch succeeded.
        """
        from .parser import Handoff  # avoid circular import at module level

        agent = self._agents.get(agent_name)
        if not agent:
            log.error(f"Agent '{agent_name}' not found")
            return False

        if not self.is_agent_idle(agent_name):
            log.warning(f"Agent '{agent_name}' is not idle, cannot dispatch")
            return False

        if agent_name in self._active_agents:
            log.warning(f"Agent '{agent_name}' already tracked as active")
            return False

        # Detect dispatch mode based on pane state
        try:
            cmd = agent.pane.pane_current_command
        except Exception:
            log.error(f"[{agent_name}] Cannot read pane_current_command")
            return False

        if cmd == "claude":
            return self._dispatch_interactive(agent_name, agent, handoff)
        else:
            return self._dispatch_headless(agent_name, agent, handoff, handoff_path)

    def _dispatch_interactive(
        self,
        agent_name: str,
        agent: AgentPane,
        handoff: "Handoff",
    ) -> bool:
        """Dispatch work to an interactive Claude Code session.

        Sends Ctrl+U (clear any partial input) then `/workflow-next`
        to the existing interactive session at the `>` prompt.
        """
        try:
            # Clear any partially typed text
            agent.pane.send_keys("C-u", enter=False, suppress_history=True)
            import time as _time
            _time.sleep(0.1)

            # Send /workflow-next command
            agent.pane.send_keys("/workflow-next", enter=True, suppress_history=False)

            self._active_agents.add(agent_name)
            log.info(
                f"[{agent_name}] Dispatched /workflow-next to interactive session "
                f"(spec={handoff.spec_id}, seq={handoff.sequence})"
            )
            return True
        except Exception as e:
            log.error(f"[{agent_name}] Interactive dispatch failed: {e}")
            return False

    def _dispatch_headless(
        self,
        agent_name: str,
        agent: AgentPane,
        handoff: "Handoff",
        handoff_path: Path,
    ) -> bool:
        """Dispatch work via headless `claude -p` (original mode).

        1. Build prompt file with handoff context
        2. Launch: cat prompt.md | claude -p ... 2>&1 | tee log
        3. Track as active
        """
        # Build prompt file
        prompt_path = self._build_prompt_file(agent_name, handoff, handoff_path)
        if not prompt_path:
            return False

        # Resolve skill path
        skill_path = self._get_skill_path(agent_name)
        skill_flag = (
            f"--append-system-prompt-file {skill_path}"
            if skill_path
            else ""
        )

        # Build log file path
        seq = handoff.sequence
        log_file = self.config.log_dir / f"agent-{agent_name}-{seq:03d}.log"

        # Build the full command
        project_root = self.config.project_root
        cmd = (
            f"{CLEAN_ENV_PREFIX} "
            f"cd {project_root} && "
            f"cat {prompt_path} | claude -p "
            f"--output-format stream-json "
            f"--permission-mode bypassPermissions "
            f"{skill_flag} "
            f"2>&1 | tee {log_file}"
        )

        # Send to pane
        success = agent.send_command(cmd)
        if success:
            self._active_agents.add(agent_name)
            log.info(
                f"[{agent_name}] Dispatched headless claude -p "
                f"(spec={handoff.spec_id}, seq={seq})"
            )
        else:
            log.error(f"[{agent_name}] Failed to send command to pane")

        return success

    def _build_prompt_file(
        self,
        agent_name: str,
        handoff: "Handoff",
        handoff_path: Path,
    ) -> Optional[Path]:
        """Write handoff context to a prompt file for stdin piping.

        Returns path to the prompt file, or None on failure.
        """
        try:
            # Read the raw handoff JSON for inclusion
            handoff_json = handoff_path.read_text()

            prompt_content = f"""## Handoff Details

Agent: {agent_name}
Spec: {handoff.spec_id}
Sequence: {handoff.sequence}
From: {handoff.source_agent}
Action: {handoff.nextAgent.action}

## Handoff JSON

```json
{handoff_json}
```

## Your Task

{handoff.nextAgent.action}

{handoff.nextAgent.context or "No additional context provided."}

## Instructions

1. Read the handoff JSON above carefully.
2. Execute the requested action.
3. When complete, write your handoff JSON to: docs/agent-comms/
4. Follow the naming convention: SPECID-SEQ-YOURAGENT-to-NEXTAGENT.json
"""
            # Write prompt file
            timestamp = int(time.time())
            prompt_path = self._prompt_dir / f"{agent_name}-{handoff.sequence:03d}-{timestamp}.md"
            prompt_path.write_text(prompt_content)
            log.debug(f"Wrote prompt file: {prompt_path}")
            return prompt_path

        except Exception as e:
            log.error(f"Failed to build prompt file for {agent_name}: {e}")
            return None

    def _get_skill_path(self, agent_name: str) -> Optional[Path]:
        """Resolve the SKILL.md path for an agent."""
        skill_dir_name = SKILL_DIR_MAP.get(agent_name)
        if not skill_dir_name:
            log.debug(f"No skill mapping for agent '{agent_name}'")
            return None

        skill_path = (
            self.config.project_root
            / ".claude" / "skills" / skill_dir_name / "SKILL.md"
        )
        if not skill_path.exists():
            log.warning(f"Skill file not found: {skill_path}")
            return None

        return skill_path

    # ------------------------------------------------------------------
    # Completion Detection
    # ------------------------------------------------------------------

    def check_completions(self) -> list[str]:
        """Poll all active agents for completion.

        Dual-mode: an agent is complete when it returns to idle state:
          - Headless mode: pane_current_command returns to bash/shell
          - Interactive mode: pane_current_command is "claude" and
            the `>` prompt is visible again

        Note: is_agent_idle() filters out agents in _active_agents for
        interactive mode (to prevent re-dispatch). Completion detection
        must bypass that filter, so we check directly here.

        Returns list of agent names that have completed.
        """
        completed = []
        for agent_name in list(self._active_agents):
            if self._is_agent_complete(agent_name):
                completed.append(agent_name)
                self._active_agents.discard(agent_name)

                # Try to extract session ID from log (headless mode only)
                self._try_extract_session_id(agent_name)

                log.info(f"[{agent_name}] Completed (pane idle)")

        return completed

    def _is_agent_complete(self, agent_name: str) -> bool:
        """Check if an active agent has finished its work.

        Unlike is_agent_idle(), this does NOT filter by _active_agents.
        It's called from check_completions() for agents we KNOW are active
        to detect when they return to idle.

          - Headless: pane_current_command ∈ IDLE_SHELLS (back to bash)
          - Interactive: pane_current_command == "claude" + `>` prompt visible
        """
        agent = self._agents.get(agent_name)
        if not agent:
            return False
        try:
            cmd = agent.pane.pane_current_command
            if cmd in IDLE_SHELLS:
                return True
            if cmd == "claude":
                return self._is_interactive_prompt(agent)
            return False
        except Exception:
            return False

    def _try_extract_session_id(self, agent_name: str):
        """Parse the most recent log file for session_id."""
        import json
        try:
            # Find most recent log file for this agent
            pattern = f"agent-{agent_name}-*.log"
            log_files = sorted(self.config.log_dir.glob(pattern))
            if not log_files:
                return

            log_file = log_files[-1]
            for line in log_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if (isinstance(obj, dict)
                            and obj.get("type") == "system"
                            and obj.get("subtype") == "init"
                            and "session_id" in obj):
                        self._session_ids[agent_name] = obj["session_id"]
                        log.debug(
                            f"[{agent_name}] Session ID: {obj['session_id']}"
                        )
                        return
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            log.debug(f"Failed to extract session_id for {agent_name}: {e}")

    # ------------------------------------------------------------------
    # Agent 007 (headless, same pattern)
    # ------------------------------------------------------------------

    def launch_agent_007(self, prompt_file: Path) -> bool:
        """Launch Agent 007 in its pane via headless claude -p.

        Agent 007 is a supervisory agent that already uses headless dispatch.
        This method aligns it with the same pattern as regular agents.
        """
        agent = self._agents.get("agent-007")
        if not agent:
            log.error("Agent 007 pane not found")
            return False

        if not self.is_agent_idle("agent-007"):
            log.warning("Agent 007 is not idle")
            return False

        project_root = self.config.agent_007_project_root
        budget = self.config.agent_007_budget_usd
        skill_path = (
            project_root / ".claude" / "skills"
            / "agent-007-supervisor" / "SKILL.md"
        )
        skill_flag = (
            f"--append-system-prompt-file {skill_path}"
            if skill_path.exists()
            else ""
        )

        log_file = self.config.log_dir / f"agent-007-{int(time.time())}.log"

        cmd = (
            f"{CLEAN_ENV_PREFIX} "
            f"cd {project_root} && "
            f"cat {prompt_file} | claude -p "
            f"--output-format stream-json "
            f"--permission-mode bypassPermissions "
            f"--max-budget-usd {budget} "
            f"{skill_flag} "
            f"--no-session-persistence "
            f"2>&1 | tee {log_file}"
        )

        success = agent.send_command(cmd)
        if success:
            self._active_agents.add("agent-007")
            log.info(f"[agent-007] Launched (budget=${budget})")
        return success

    def check_agent_007_idle(self) -> bool:
        """Check if Agent 007 pane is idle.

        Deterministic: pane_current_command ∈ IDLE_SHELLS.
        Replaces regex-based prompt detection.
        """
        return self.is_agent_idle("agent-007")

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_agent(self, name: str) -> Optional[AgentPane]:
        """Get an AgentPane by name."""
        return self._agents.get(name)

    @property
    def active_agents(self) -> set[str]:
        """Set of agent names with running claude -p processes."""
        return self._active_agents.copy()

    @property
    def agents(self) -> dict[str, "AgentPane"]:
        """All discovered agent panes (name → AgentPane).

        Used by daemon._init_agent_states() and tui agent count display.
        Returns a shallow copy to prevent external mutation.
        """
        return dict(self._agents)

    @property
    def discovered_agents(self) -> list[str]:
        """List of all discovered agent names."""
        return list(self._agents.keys())
