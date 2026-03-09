"""HeadlessCommander — Headless-only agent dispatch.

All agent panes start as idle bash shells. When IWO detects a handoff
targeting an agent, it launches `claude -p` in the pane with the handoff
context. When claude -p exits, the pane returns to idle bash.

Dispatch:
    cd $PROJECT && cat prompt.md | claude -p \\
        --model opus \\
        --output-format stream-json \\
        --permission-mode bypassPermissions \\
        --append-system-prompt-file .claude/skills/$SKILL/SKILL.md \\
        2>&1 | tee $LOG

Idle detection: pane_current_command ∈ IDLE_SHELLS AND no child processes
(pgrep -P $pane_pid). The child-process check catches claude -p which tmux
reports as "bash" because it runs as a child of the shell process.
No interactive prompt matching, no canary probes, no send-keys injection.
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

# IWO agent name → Claude model to use for headless dispatch.
# Planner/Builder/Reviewer need Opus for quality; Tester/Deployer/Docs
# can use Sonnet for speed since their tasks are more mechanical.
AGENT_MODEL_MAP: dict[str, str] = {
    "planner": "opus",
    "builder": "opus",
    "reviewer": "opus",
    "tester": "sonnet",
    "deployer": "sonnet",
    "docs": "sonnet",
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

    # Minimum seconds between dispatch attempts to the same agent.
    # Prevents rapid C-u/workflow-next injection from poll loops and
    # state recovery bursts.
    _DISPATCH_COOLDOWN_SECONDS = 10.0

    # Exponential backoff for failed dispatch attempts.
    # After a dispatch failure, subsequent attempts are delayed:
    #   cooldown = min(BASE * 2^(fail_count-1), MAX)
    # i.e. 30s → 60s → 120s → 300s → 300s ...
    # Resets to zero on successful dispatch.
    _DISPATCH_FAIL_BASE_COOLDOWN = 30.0
    _DISPATCH_FAIL_MAX_COOLDOWN = 300.0

    def __init__(self, config: IWOConfig):
        self.config = config
        self._server: Optional[libtmux.Server] = None
        self._session: Optional[libtmux.Session] = None
        self._agents: dict[str, AgentPane] = {}

        # Track which agents currently have a claude -p process running
        self._active_agents: set[str] = set()

        # Session IDs from stream-json output (for potential resumption)
        self._session_ids: dict[str, str] = {}

        # Dispatch rate limiting: agent_name → last dispatch timestamp
        self._last_dispatch_time: dict[str, float] = {}

        # Failed dispatch backoff: tracks consecutive failures per agent
        self._dispatch_fail_count: dict[str, int] = {}
        self._dispatch_fail_time: dict[str, float] = {}

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
    # Pane Identity Validation (dispatch safety)
    # ------------------------------------------------------------------

    def _validate_pane_identity(self, agent_name: str, agent: AgentPane) -> bool:
        """Verify a pane is genuinely an IWO agent before dispatching.

        Three checks prevent injecting C-u/workflow-next into wrong panes:

        1. **Tag check**: The pane must have the @iwo-agent user option set
           to this agent's name.  Without the tag, the pane was discovered
           by window-index fallback and may be an unrelated session.

        2. **Working directory check**: The pane's current working directory
           must be within the configured project_root.  An agent pane
           running ebatt code should be in the ebatt project tree.

        3. **Rate limiting**: Dispatch to the same agent must not happen
           more than once per _DISPATCH_COOLDOWN_SECONDS to prevent
           rapid repeated C-u/workflow-next injection from poll loops.

        Returns True only if all checks pass.
        """
        import subprocess

        # --- Check 1: @iwo-agent tag matches ---
        try:
            result = subprocess.run(
                ["tmux", "show-options", "-p", "-t", agent.pane.pane_id,
                 "-v", self.config.pane_tag_key],
                capture_output=True, text=True, timeout=5,
            )
            tag_value = result.stdout.strip() if result.returncode == 0 else ""
            if tag_value != agent_name:
                log.warning(
                    f"[{agent_name}] Pane identity REJECTED: "
                    f"@iwo-agent tag is {tag_value!r}, expected {agent_name!r} "
                    f"(pane {agent.pane.pane_id}). "
                    f"This pane may not be a Boris agent — refusing dispatch."
                )
                return False
        except Exception as e:
            log.warning(
                f"[{agent_name}] Cannot verify @iwo-agent tag "
                f"(pane {agent.pane.pane_id}): {e} — refusing dispatch"
            )
            return False

        # --- Check 2: Working directory within project root ---
        try:
            pane_path = agent.pane.pane_current_path
            if pane_path:
                project_root_str = str(self.config.project_root)
                if not pane_path.startswith(project_root_str):
                    log.warning(
                        f"[{agent_name}] Pane identity REJECTED: "
                        f"pane cwd is {pane_path!r}, expected prefix "
                        f"{project_root_str!r} — refusing dispatch"
                    )
                    return False
        except Exception as e:
            # pane_current_path may not be available — log but don't block
            log.debug(f"[{agent_name}] Could not check pane cwd: {e}")

        # --- Check 3: Rate limiting ---
        now = time.time()
        last = self._last_dispatch_time.get(agent_name, 0.0)
        elapsed = now - last
        if elapsed < self._DISPATCH_COOLDOWN_SECONDS:
            log.warning(
                f"[{agent_name}] Dispatch THROTTLED: only {elapsed:.1f}s "
                f"since last dispatch (cooldown={self._DISPATCH_COOLDOWN_SECONDS}s)"
            )
            return False

        log.debug(
            f"[{agent_name}] Pane identity VERIFIED: "
            f"tag={agent_name!r}, pane={agent.pane.pane_id}"
        )
        return True

    # ------------------------------------------------------------------
    # Idle Detection (deterministic)
    # ------------------------------------------------------------------

    def is_agent_idle(self, agent_name: str) -> bool:
        """Check if agent pane is idle (running a shell, no child processes).

        Two-layer check:
        1. pane_current_command ∈ IDLE_SHELLS (fast path)
        2. Shell has no child processes (catches claude -p which tmux
           reports as "bash" because it's a child of the shell)

        Without check 2, tmux's pane_current_command returns "bash" even
        while `claude -p` is running as a child, causing double-dispatch.
        """
        agent = self._agents.get(agent_name)
        if not agent:
            log.debug(f"[{agent_name}] not in _agents — cannot check idle")
            return False
        try:
            cmd = agent.pane.pane_current_command
            log.debug(f"[{agent_name}] pane_current_command = {cmd!r}")
            if cmd not in IDLE_SHELLS:
                return False

            # Check 2: verify the shell has no child processes.
            # pane_pid is the shell PID; if claude -p is running, it's a
            # child of that shell and pgrep will find it.
            pane_pid = agent.pane.pane_pid
            if pane_pid:
                import subprocess
                result = subprocess.run(
                    ["pgrep", "-P", str(pane_pid)],
                    capture_output=True, text=True, timeout=3,
                )
                if result.stdout.strip():
                    # Shell has child processes — not idle
                    log.debug(
                        f"[{agent_name}] pane_current_command=bash but shell "
                        f"(PID {pane_pid}) has children: "
                        f"{result.stdout.strip().replace(chr(10), ', ')} "
                        f"— NOT idle"
                    )
                    return False

            return True
        except Exception as e:
            log.warning(f"[{agent_name}] pane read failed: {e}")
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
        """Dispatch work to an agent via headless claude -p.

        Requires the agent pane to be at an idle shell prompt.
        If the pane is running claude or any other process, dispatch
        is refused — the agent must finish or be terminated first.

        Failed dispatches trigger exponential backoff (30s → 60s → 120s →
        300s max) to prevent the 2-second poll loop from hammering a pane.

        Returns True if dispatch succeeded.
        """
        from .parser import Handoff  # avoid circular import at module level

        agent = self._agents.get(agent_name)
        if not agent:
            log.error(f"Agent '{agent_name}' not found")
            return False

        # --- Failed dispatch backoff ---
        fail_count = self._dispatch_fail_count.get(agent_name, 0)
        if fail_count > 0:
            fail_time = self._dispatch_fail_time.get(agent_name, 0.0)
            cooldown = min(
                self._DISPATCH_FAIL_BASE_COOLDOWN * (2 ** (fail_count - 1)),
                self._DISPATCH_FAIL_MAX_COOLDOWN,
            )
            elapsed = time.time() - fail_time
            if elapsed < cooldown:
                log.info(
                    f"[{agent_name}] Dispatch BACKOFF: {fail_count} consecutive "
                    f"failures, {cooldown - elapsed:.0f}s remaining "
                    f"(cooldown={cooldown:.0f}s)"
                )
                return False

        if not self.is_agent_idle(agent_name):
            log.warning(f"Agent '{agent_name}' is not idle, cannot dispatch")
            return False

        if agent_name in self._active_agents:
            log.warning(f"Agent '{agent_name}' already tracked as active")
            return False

        # --- Pane identity validation ---
        if not self._validate_pane_identity(agent_name, agent):
            log.error(
                f"[{agent_name}] Dispatch ABORTED: pane identity check failed."
            )
            self._record_dispatch_failure(agent_name)
            return False

        # --- Headless dispatch (only mode) ---
        success = self._dispatch_headless(agent_name, agent, handoff, handoff_path)

        if success:
            self._dispatch_fail_count.pop(agent_name, None)
            self._dispatch_fail_time.pop(agent_name, None)
        else:
            self._record_dispatch_failure(agent_name)

        return success

    def _record_dispatch_failure(self, agent_name: str):
        """Record a dispatch failure for exponential backoff tracking."""
        count = self._dispatch_fail_count.get(agent_name, 0) + 1
        self._dispatch_fail_count[agent_name] = count
        self._dispatch_fail_time[agent_name] = time.time()
        cooldown = min(
            self._DISPATCH_FAIL_BASE_COOLDOWN * (2 ** (count - 1)),
            self._DISPATCH_FAIL_MAX_COOLDOWN,
        )
        log.warning(
            f"[{agent_name}] Dispatch failure #{count} — "
            f"next attempt in {cooldown:.0f}s"
        )

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
        model = AGENT_MODEL_MAP.get(agent_name, "sonnet")
        cmd = (
            f"{CLEAN_ENV_PREFIX} "
            f"cd {project_root} && "
            f"cat {prompt_path} | claude -p "
            f"--model {model} "
            f"--output-format stream-json "
            f"--permission-mode bypassPermissions "
            f"{skill_flag} "
            f"2>&1 | tee {log_file}"
        )

        # Send to pane
        success = agent.send_command(cmd)
        if success:
            self._active_agents.add(agent_name)
            self._last_dispatch_time[agent_name] = time.time()
            log.info(
                f"[{agent_name}] Dispatched headless claude -p "
                f"(spec={handoff.spec_id}, seq={seq}, model={model})"
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

        An agent is complete when pane_current_command returns to a shell
        (claude -p has exited). Returns list of agent names that completed.
        """
        completed = []
        for agent_name in list(self._active_agents):
            if self._is_agent_complete(agent_name):
                completed.append(agent_name)
                self._active_agents.discard(agent_name)
                self._try_extract_session_id(agent_name)
                log.info(f"[{agent_name}] Completed (pane returned to shell)")
        return completed

    def _is_agent_complete(self, agent_name: str) -> bool:
        """Check if an active agent has finished (pane back at shell, no children).

        Uses is_agent_idle() which checks both pane_current_command AND
        child processes to avoid false completion detection.
        """
        return self.is_agent_idle(agent_name)

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

        If agent-007 was not discovered during initial connect(), retries
        discovery once before giving up (lazy re-discovery).
        """
        agent = self._agents.get("agent-007")
        if not agent:
            # Lazy re-discovery: tmux window may not have existed at startup
            log.info("[agent-007] Not in _agents, attempting re-discovery...")
            self._discover_agent_007()
            agent = self._agents.get("agent-007")
            if not agent:
                log.error(
                    "[agent-007] Pane not found even after re-discovery. "
                    f"Expected window index {self.config.agent_007_window} "
                    f"in session '{self.config.tmux_session_name}'"
                )
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
