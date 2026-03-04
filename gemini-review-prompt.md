# Architectural Review Request: IWO Daemon Retry Hardening

## Context

You are reviewing **IWO (Ivan's Workflow Orchestrator)** — a Python daemon (~10 modules) that automates handoffs between 6 Claude Code AI agents running in tmux panes. The agents follow the Boris Cherny 6-agent pipeline: **Planner → Builder → Reviewer → Tester → Deployer → Docs**.

IWO watches a filesystem directory for handoff JSON files. When a new handoff arrives (e.g., Builder → Reviewer), IWO parses it, identifies the target agent, checks if that agent's tmux pane is idle, and dispatches work to it.

### Dispatch Modes

IWO supports two dispatch modes:

1. **Headless** (`claude -p`): When the agent pane is at a bash prompt, IWO pipes a prompt file into `claude -p` via stdin.
2. **Interactive** (`/workflow-next`): When the agent pane has an active Claude Code TUI session at its `❯` prompt, IWO sends `Ctrl+U` (clear line) then `/workflow-next` (Enter) via tmux `send_keys`.

### The Bug (now partially fixed)

The daemon's 2-second poll loop was **infinitely re-injecting `C-u` + `/workflow-next`** keystrokes into tmux panes that were actively working. This is catastrophically destructive — `Ctrl+U` clears the current input line in any terminal application, and `/workflow-next` is a Claude Code slash command that interrupts whatever the agent is doing.

**Root cause**: Two failures compounded:
1. The daemon's `_activate_for_handoff()` unconditionally re-queues failed dispatches with no retry limit
2. The commander's `activate_agent()` had no pane identity validation or rate limiting

### Fix Already Applied (Commander-Side)

We've already implemented and merged a **three-layer pane identity validation** in `headless_commander.py`:

1. **`@iwo-agent` tag check**: Verifies the tmux pane's user option matches the expected agent name
2. **Working directory check**: Confirms the pane's cwd is within the project root
3. **10-second rate limiting cooldown**: Prevents dispatching to the same agent more than once per 10 seconds

Plus **exponential backoff** on consecutive dispatch failures: 30s → 60s → 120s → 300s max, resetting on success.

### What's NOT Yet Fixed (Daemon-Side)

The daemon's `_activate_for_handoff()` method (line 522-551 in `daemon.py`) still has an **infinite retry loop**:

```python
def _activate_for_handoff(self, agent: str, handoff: Handoff, path: Path):
    success = self.commander.activate_agent(agent, handoff=handoff, handoff_path=path)
    if success:
        # Mark agent as processing, update pipeline...
    else:
        # Re-queue on failure — will retry next poll cycle
        self.pipeline.enqueue(handoff, path)
        self._notify(f"❌ Failed to activate {agent}, re-queued", critical=True)
```

When `activate_agent()` returns `False`, the handoff is immediately re-queued. The `_process_pending_activations()` method (line 489-520) dequeues and calls `_activate_for_handoff()` again every 2-second poll cycle. With the commander-side backoff, the retry rate is now bounded by the 30s-300s cooldown, but:

- **There is no maximum retry count** — a permanently broken pane retries forever
- **There is no failure classification** — transient failures (pane busy) get the same treatment as permanent ones (pane gone, tag missing)
- **There is no dead-letter queue** — failed handoffs cycle indefinitely
- **There is no deduplication in the pipeline queue** — the same handoff can be enqueued multiple times

### GPT-5.2's Prior Review (Key Findings)

A GPT-5.2 peer review identified these additional vulnerabilities:

1. **Daemon-side retry has no limits** (most critical) — the re-queue in `_activate_for_handoff()` creates an infinite retry loop. Even with commander-side backoff, this is defense-in-depth failure.
2. **TOCTOU race condition** — between `_validate_pane_identity()` returning True and `send_keys()` executing, the pane could be swapped. The window is ~100ms but exists.
3. **No failure classification** — "agent busy" (transient, will resolve) vs. "tag mismatch" (permanent, needs human intervention) should be handled differently.
4. **`C-u` is inherently destructive** — even with validation, `C-u` clears input in any terminal state. If the pane happens to be at a bash prompt with important partial input, it's lost.
5. **Queue deduplication missing** — `pipeline.enqueue()` doesn't check for duplicate entries.

---

## Source Code

### File 1: `iwo/headless_commander.py` (870 lines — FIXED)

This is the commander layer that actually dispatches to tmux panes. The three-layer validation and exponential backoff are already implemented here.

```python
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
  - pane_current_command ∈ CLAUDE_COMMANDS + `❯` prompt visible → interactive idle

Design: Three-model consensus (Claude Opus 4.6, GPT-5.2, Gemini 3 Pro).
"""

import logging
import re
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

# Claude Code process names — "claude" is the binary name, but Claude Code
# is a Node.js app so tmux may report "node" as pane_current_command.
CLAUDE_COMMANDS = frozenset(("claude", "node"))

# Regex to strip ANSI escape sequences from captured pane output.
# capture-pane -p -J does NOT strip ANSI codes, so prompt matching
# like `stripped == ">"` fails when the prompt has colour codes.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]")

# Claude Code prompt characters.  The TUI uses ❯ (U+276F "HEAVY RIGHT-POINTING
# ANGLE QUOTATION MARK ORNAMENT") rather than ASCII >.  Match both to be safe.
_PROMPT_CHARS = frozenset(("❯", ">"))

# Claude Code status chrome — lines belonging to the bottom status bar
# that must be skipped when scanning for the prompt.  Patterns:
#   ⏵⏵  = permission mode bar      (U+23F5 BLACK MEDIUM RIGHT-POINTING TRIANGLE)
#   ───  = box-drawing divider      (U+2500 BOX DRAWINGS LIGHT HORIZONTAL)
#   token counter / path bar        (contains " tokens" or "@" agent-node)
_CHROME_INDICATORS = ("⏵", "─", " tokens", "current:", "latest:")

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
        """Check if agent pane is idle and ready for dispatch.

        Dual-mode detection:
          1. pane_current_command ∈ IDLE_SHELLS → bash idle (headless mode)
          2. pane_current_command == "claude" AND interactive `>` prompt
             visible AND agent not in _active_agents → interactive idle

        Returns True if the agent can accept new work.
        """
        agent = self._agents.get(agent_name)
        if not agent:
            log.debug(f"[{agent_name}] not in _agents — cannot check idle")
            return False
        try:
            cmd = agent.pane.pane_current_command
            log.debug(f"[{agent_name}] pane_current_command = {cmd!r}")
            # Mode 1: bash/shell idle — ready for headless claude -p
            if cmd in IDLE_SHELLS:
                return True
            # Mode 2: interactive Claude session at `>` prompt
            # Claude Code is Node.js, so tmux may report "node" or "claude"
            if cmd in CLAUDE_COMMANDS and agent_name not in self._active_agents:
                return self._is_interactive_prompt(agent)
            return False
        except Exception as e:
            log.warning(f"[{agent_name}] pane read failed: {e}")
            return False

    def _is_interactive_prompt(self, agent: AgentPane) -> bool:
        """Check if an interactive Claude Code session is at its ``❯`` prompt.

        Claude Code's TUI layout (bottom to top):
            [empty / version info]
            ⏵⏵ bypass permissions on ...         ← permission bar (chrome)
            vanya@host:/path/...  123456 tokens  ← path/token bar (chrome)
            ────────────────────────────────────  ← divider        (chrome)
            ❯ /workflow-next                      ← THE PROMPT
            ────────────────────────────────────  ← divider        (chrome)
            ... output content above ...

        We scan the last ~20 lines from the bottom, skip empty lines
        and Claude Code status chrome, then look for the prompt character
        ``❯`` (U+276F) or ``>`` (ASCII fallback).
        """
        try:
            lines = agent.capture_visible(last_n_lines=20)
            for line in reversed(lines):
                # Strip ANSI escape codes THEN whitespace
                cleaned = _ANSI_ESCAPE_RE.sub("", line)
                stripped = cleaned.strip()
                if not stripped:
                    continue
                # Skip Claude Code status chrome (status bar, dividers, etc.)
                if any(stripped.startswith(ind) for ind in _CHROME_INDICATORS):
                    continue
                # Also skip lines that look like the path/token status bar
                if "@" in stripped and ("tokens" in stripped or ":" in stripped[:40]):
                    continue
                # Check for Claude Code prompt: ❯ or > (with optional text after)
                first_char = stripped[0]
                if first_char in _PROMPT_CHARS:
                    log.debug(f"[{agent.agent_name}] interactive prompt detected")
                    return True
                # Non-chrome, non-prompt content → agent is outputting
                log.debug(
                    f"[{agent.agent_name}] not at prompt, last line: {stripped[:60]!r}"
                )
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

        Failed dispatches trigger exponential backoff (30s → 60s → 120s →
        300s max) to prevent the 2-second poll loop from hammering a pane
        that keeps rejecting commands.

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

        # --- Pane identity validation (prevents C-u/workflow-next injection) ---
        if not self._validate_pane_identity(agent_name, agent):
            log.error(
                f"[{agent_name}] Dispatch ABORTED: pane identity check failed. "
                f"Refusing to send commands to unverified pane."
            )
            self._record_dispatch_failure(agent_name)
            return False

        # Detect dispatch mode based on pane state
        try:
            cmd = agent.pane.pane_current_command
        except Exception:
            log.error(f"[{agent_name}] Cannot read pane_current_command")
            self._record_dispatch_failure(agent_name)
            return False

        if cmd in CLAUDE_COMMANDS:
            success = self._dispatch_interactive(agent_name, agent, handoff)
        else:
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

    # Maximum attempts for interactive dispatch (send_keys is fire-and-forget)
    _INTERACTIVE_DISPATCH_MAX_RETRIES = 2
    _INTERACTIVE_DISPATCH_VERIFY_DELAY = 0.5  # seconds after send_keys
    _INTERACTIVE_DISPATCH_CLEAR_DELAY = 0.3   # seconds after C-u

    def _dispatch_interactive(
        self,
        agent_name: str,
        agent: AgentPane,
        handoff: "Handoff",
    ) -> bool:
        """Dispatch work to an interactive Claude Code session.

        Sends Ctrl+U (clear any partial input) then ``/workflow-next``
        to the existing interactive session at the ``❯`` prompt.

        After sending, verifies delivery by checking that the agent is
        no longer at the idle prompt (i.e. it started processing the
        command).  Retries up to ``_INTERACTIVE_DISPATCH_MAX_RETRIES``
        times if the prompt is still visible after sending.
        """
        max_retries = self._INTERACTIVE_DISPATCH_MAX_RETRIES

        for attempt in range(1, max_retries + 1):
            try:
                # Clear any partially typed text
                agent.pane.send_keys("C-u", enter=False, suppress_history=True)
                time.sleep(self._INTERACTIVE_DISPATCH_CLEAR_DELAY)

                # Send /workflow-next command
                agent.pane.send_keys(
                    "/workflow-next", enter=True, suppress_history=False,
                )
                log.info(
                    f"[{agent_name}] Sent /workflow-next (attempt {attempt}/"
                    f"{max_retries}, spec={handoff.spec_id}, "
                    f"seq={handoff.sequence})"
                )

                # Verify: wait then check that the prompt has gone away
                time.sleep(self._INTERACTIVE_DISPATCH_VERIFY_DELAY)
                if not self._is_interactive_prompt(agent):
                    # Agent is no longer at the prompt → delivery confirmed
                    self._active_agents.add(agent_name)
                    self._last_dispatch_time[agent_name] = time.time()
                    log.info(
                        f"[{agent_name}] Verified: agent accepted /workflow-next "
                        f"on attempt {attempt}"
                    )
                    return True

                # Still at prompt — command may have been swallowed
                log.warning(
                    f"[{agent_name}] Prompt still visible after attempt "
                    f"{attempt}/{max_retries} — retrying"
                )
            except Exception as e:
                log.error(
                    f"[{agent_name}] Interactive dispatch error on attempt "
                    f"{attempt}: {e}"
                )

        log.error(
            f"[{agent_name}] Interactive dispatch FAILED after {max_retries} "
            f"attempts — agent may not have received /workflow-next"
        )
        return False

    # ... (headless dispatch, completion detection, Agent 007 — omitted for brevity,
    #      these methods don't have the retry bug)
```

### File 2: `iwo/daemon.py` (1232 lines — THE UNFIXED RETRY LOOP)

This is the main daemon. Focus on `_activate_for_handoff()` (line 522) and `_process_pending_activations()` (line 489).

```python
"""IWO Daemon — Headless dispatch orchestrator.

Watches for handoff files, validates them, and dispatches to agents
via HeadlessCommander (deterministic ``claude -p`` subprocess invocation).

Key capabilities:
- HeadlessCommander dispatch (Phase 3): deterministic idle detection via
  pane_current_command, no canary probes or regex prompt matching
- Multi-spec pipeline tracking (PipelineManager) with rejection-first priority
- Automatic crash recovery (respawn-pane + re-launch Claude Code)
- Deploy gate with TUI manual approval flow
- Post-deploy health checks
- 30-second filesystem reconciliation
- Pipe-pane archival logging
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
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

from .config import IWOConfig
from .parser import Handoff
from .headless_commander import HeadlessCommander
from .state import AgentState
from .memory import IWOMemory
from .pipeline import PipelineManager
from .metrics import MetricsCollector
from .auditor import Auditor, AuditorConfig

log = logging.getLogger("iwo.daemon")


class HandoffTracker:
    """Tracks processed handoffs to prevent duplicates (GPT-5.2's idempotency key).

    Phase 2.7: Supports supersede — if a newer file arrives with the same
    idempotency key, it replaces the original (e.g., Reviewer redoes work).
    """

    def __init__(self):
        self._processed: set[str] = set()
        self._processed_paths: dict[str, Path] = {}
        self._spec_handoff_counts: dict[str, int] = {}
        self._rejection_counts: dict[str, int] = {}

    def already_processed(self, handoff: Handoff, path: Optional[Path] = None) -> bool:
        key = handoff.idempotency_key
        if key not in self._processed:
            return False
        if path and key in self._processed_paths:
            prev_path = self._processed_paths[key]
            if path != prev_path:
                log.info(f"Supersede: {key} has newer file {path.name}")
                self._processed.discard(key)
                return False
        return True

    def mark_processed(self, handoff: Handoff, path: Optional[Path] = None):
        self._processed.add(handoff.idempotency_key)
        if path:
            self._processed_paths[handoff.idempotency_key] = path
        spec = handoff.spec_id
        self._spec_handoff_counts[spec] = self._spec_handoff_counts.get(spec, 0) + 1

    def check_rejection_loop(self, handoff: Handoff, max_loops: int) -> bool:
        if not handoff.is_rejection:
            return False
        key = f"{handoff.spec_id}:{handoff.source_agent}->{handoff.target_agent}"
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        return self._rejection_counts[key] >= max_loops

    def check_handoff_limit(self, handoff: Handoff, max_handoffs: int) -> bool:
        return self._spec_handoff_counts.get(handoff.spec_id, 0) >= max_handoffs


class IWODaemon:
    """Main orchestrator daemon."""

    def __init__(self, config: Optional[IWOConfig] = None):
        self.config = config or IWOConfig()
        self.commander = HeadlessCommander(self.config)
        self.tracker = HandoffTracker()
        self.observer: Optional[Observer] = None

        self.agent_states: dict[str, AgentState] = {}
        self._state_changed_at: dict[str, float] = {}
        self._pending_activations: list[tuple[Handoff, Path]] = []
        self.handoff_history: list[Handoff] = []
        self._max_history: int = 50
        self._started_at: float = time.time()
        self._session_id: str = time.strftime("%Y%m%d-%H%M%S")

        self.memory: Optional[IWOMemory] = None
        self.pipeline = PipelineManager(max_concurrent=self.config.max_concurrent_specs)
        self.metrics: Optional[MetricsCollector] = None
        self.auditor: Optional[Auditor] = None

        # ... (notification debounce, respawn tracking, deploy gate — omitted)

    def _process_pending_activations(self):
        """Drain queued handoffs to idle agents.

        ⚠️ THIS IS THE RETRY PUMP — called every 2s from _poll_agent_states().
        For each idle agent with queued work, dequeues and calls
        _activate_for_handoff(), which re-enqueues on failure.
        """
        # Legacy pending list migration
        if self._pending_activations:
            for handoff, path in self._pending_activations:
                self.pipeline.enqueue(handoff, path)
            self._pending_activations.clear()

        # Check each agent's queue
        for name in list(self.agent_states.keys()):
            if self.pipeline.queue_depth(name) == 0:
                continue
            if self.pipeline.is_agent_busy(name):
                continue
            if not self.commander.is_agent_idle(name):
                continue

            queued = self.pipeline.dequeue(name)
            if queued:
                log.info(f"Queue drain: {name} idle — dispatching {queued.spec_id}")
                self._activate_for_handoff(name, queued.handoff, queued.path)

    def _activate_for_handoff(self, agent: str, handoff: Handoff, path: Path):
        """Send activation command to an agent and update pipeline tracking.

        ⚠️ THE INFINITE RETRY BUG: on failure, unconditionally re-queues
        the handoff. _process_pending_activations() will dequeue and retry
        on the next 2s poll cycle. There is NO:
        - Maximum retry count
        - Failure classification (transient vs permanent)
        - Dead-letter queue for permanently failed handoffs
        - Queue deduplication check before re-enqueue
        """
        log.info(f"Activating {agent} for {handoff.spec_id} #{handoff.sequence}")
        success = self.commander.activate_agent(agent, handoff=handoff, handoff_path=path)
        if success:
            self.agent_states[agent] = AgentState.PROCESSING
            self._state_changed_at[agent] = time.time()
            self.pipeline.assign_agent(agent, handoff.spec_id)
            self._notify(f"✅ Activated {agent} for {handoff.spec_id} (#{handoff.sequence})")
            # ... (audit event)
        else:
            # ⚠️ UNCONDITIONAL RE-QUEUE — this is the bug
            self.pipeline.enqueue(handoff, path)
            self._notify(f"❌ Failed to activate {agent}, re-queued", critical=True)

    def _poll_agent_states(self):
        """Poll all agents for state changes. Called every ~2s from main loop.

        At the end, calls _process_pending_activations() which is
        the retry pump.
        """
        now = time.time()
        active = self.commander.active_agents

        # Check for completed agents
        completed = self.commander.check_completions()
        for name in completed:
            prev = self.agent_states.get(name, AgentState.UNKNOWN)
            self.agent_states[name] = AgentState.IDLE
            self._state_changed_at[name] = now
            # ... (state change notification)

        # Update all agent states
        for name in self.agent_states:
            if name in completed:
                continue
            prev = self.agent_states[name]
            if name in active:
                new_state = AgentState.PROCESSING
            elif self.commander.is_agent_idle(name):
                new_state = AgentState.IDLE
            else:
                new_state = AgentState.UNKNOWN
            if new_state != prev:
                self.agent_states[name] = new_state
                self._state_changed_at[name] = now

        # ⚠️ THE RETRY PUMP — drains queued handoffs to idle agents
        self._process_pending_activations()

    def process_handoff(self, path: Path):
        """Parse, validate, and route a handoff file.

        At step 9, if the target agent is idle, calls _activate_for_handoff()
        directly. If agent is busy, enqueues to pipeline queue (which
        _process_pending_activations() will drain on next poll).
        """
        # ... (parse, validate, safety rails, mark processed, etc.)

        target = handoff.target_agent

        # Route to target agent
        if target not in self.agent_states:
            self.pipeline.enqueue(handoff, path)
        else:
            if self.commander.is_agent_idle(target):
                self._activate_for_handoff(target, handoff, path)
            else:
                self.pipeline.enqueue(handoff, path)

    def run_loop(self):
        """Headless main loop: state polling + reconciliation."""
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
            self.observer.stop()
        self.observer.join()
```

### File 3: `iwo/parser.py` (194 lines — Handoff Data Model)

```python
"""Handoff JSON parser with Pydantic validation."""

from datetime import datetime
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Any, Optional


class HandoffMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    specId: str = Field(validation_alias=AliasChoices("specId", "spec"))
    agent: str
    timestamp: str
    sequence: int
    received_at: Optional[str] = None


class TestsStatus(BaseModel):
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    newTests: Optional[int] = None
    output: Optional[str] = None


class ReviewFindings(BaseModel):
    blocking: list[str] = []
    medium: list[str] = []
    low: list[str] = []


class HandoffStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    outcome: str  # "success" | "failed" | "approved"
    issueCount: int = 0
    claimMismatches: int = 0
    highSeverity: Optional[int] = None
    notes: Optional[str] = None
    goalMet: Optional[bool] = None
    unresolvedIssues: list[str] = []
    deviationsFromPlan: list[str] = []
    reviewFindings: Optional[ReviewFindings] = None


class Deliverables(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filesCreated: list[str] = []
    filesModified: list[str] = []
    filesReviewed: list[str] = []
    testsStatus: Optional[TestsStatus] = None
    typecheckPassed: Optional[bool] = None


class Evidence(BaseModel):
    reviewAreas: Optional[dict] = None
    securityCheck: Optional[str] = None
    codeQuality: Optional[str] = None


class NextAgent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: str
    action: str
    context: Optional[str] = None
    knownIssues: list[str] = []


class Handoff(BaseModel):
    """Validated handoff document matching production JSON structure."""
    model_config = ConfigDict(extra="ignore")

    metadata: HandoffMetadata
    status: HandoffStatus
    nextAgent: NextAgent
    deliverables: Optional[Deliverables] = None
    evidence: Optional[Evidence] = None
    changeSummary: Optional[dict] = None
    reviewDetails: Optional[dict] = None
    claimVerification: Optional[dict] = None
    summary: Optional[dict] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_deliverables(cls, data: Any) -> Any:
        """Normalize builder's array deliverables into Deliverables dict format."""
        if isinstance(data, dict):
            deliverables = data.get("deliverables")
            if isinstance(deliverables, list):
                created, modified = [], []
                for item in deliverables:
                    if isinstance(item, dict):
                        filepath = item.get("file", "")
                        if not filepath:
                            continue
                        if item.get("action") == "created":
                            created.append(filepath)
                        else:
                            modified.append(filepath)
                data["deliverables"] = {
                    "filesCreated": created,
                    "filesModified": modified,
                }
        return data

    @property
    def spec_id(self) -> str:
        return self.metadata.specId

    @property
    def source_agent(self) -> str:
        return self.metadata.agent

    @property
    def target_agent(self) -> str:
        return self.nextAgent.target

    @property
    def sequence(self) -> int:
        return self.metadata.sequence

    @property
    def is_rejection(self) -> bool:
        return self.status.outcome == "failed"

    @property
    def idempotency_key(self) -> str:
        return f"{self.spec_id}:{self.sequence}:{self.source_agent}:{self.target_agent}"

    @property
    def files_touched(self) -> list[str]:
        if not self.deliverables:
            return []
        return list(set(
            self.deliverables.filesCreated
            + self.deliverables.filesModified
            + self.deliverables.filesReviewed
        ))
```

---

## Your Task

Please provide a **comprehensive architectural review** of the daemon-side retry hardening needed. Specifically:

### 1. Daemon-Level Retry Limits

Design a robust retry mechanism for `_activate_for_handoff()`. Consider:

- **Maximum retry count**: What's a reasonable limit? Should it differ by failure type?
- **Dead-letter queue**: How should permanently failed handoffs be handled? Should they be moved to a quarantine directory? Logged to memory? Surfaced in the TUI?
- **Structured return types from `activate_agent()`**: Currently returns `bool`. Should it return an enum/dataclass with failure reason? How should the daemon classify failures?

### 2. Failure Classification

The commander-side `activate_agent()` can fail for many reasons:
- Agent not found (permanent)
- Agent not idle / already active (transient — will resolve when agent finishes)
- Pane identity tag mismatch (permanent — pane was replaced or misconfigured)
- Working directory check failed (permanent — wrong pane)
- Rate limit / backoff cooldown (transient — will resolve with time)
- Interactive dispatch failed after max retries (ambiguous — could be TUI state issue)
- Headless dispatch failed (transient or permanent depending on cause)

How should `activate_agent()` communicate these failure categories to the daemon? What should the daemon do differently for each?

### 3. TOCTOU Race Condition

Between `_validate_pane_identity()` returning True and `send_keys()` executing (~100ms window):
- The tmux pane could be killed and replaced
- The user could switch to a different window
- The agent could start outputting (transition from idle to working)

How serious is this race in practice? What mitigation is feasible without fundamentally changing the tmux-based architecture?

### 4. Queue Deduplication

`pipeline.enqueue()` currently doesn't check for duplicate entries. The same handoff can be re-enqueued every failed retry cycle. What deduplication strategy do you recommend? By `idempotency_key`? By `(spec_id, sequence, target_agent)` tuple?

### 5. Alternative to `C-u`

GPT-5.2 flagged that `Ctrl+U` is inherently destructive — it clears the current input line in any terminal application. Even with pane identity validation, if the validation passes but the pane state has changed between validation and `send_keys`, `C-u` could destroy user input in an interactive session.

Is there a safer alternative for clearing the tmux input line before sending `/workflow-next`? Should IWO switch to a different dispatch mechanism entirely for interactive mode?

### 6. Concrete Code Changes

Please provide **specific, production-ready code changes** for `daemon.py`'s `_activate_for_handoff()` method and any supporting infrastructure. Include:

- The modified `_activate_for_handoff()` with retry limits
- Any new data structures (DispatchResult enum, dead-letter queue, retry tracker)
- Modifications to `_process_pending_activations()` if needed
- Any changes to `activate_agent()` return type

### Constraints

- Python 3.12+, Pydantic v2, libtmux
- The solution must be backward-compatible with the existing `PipelineManager` and `HandoffTracker`
- The TUI (Textual-based) must be able to display quarantined/dead-lettered handoffs
- Changes should be minimal — surgical fixes, not a rewrite
- All code must be testable with the existing `unittest` + `unittest.mock` setup
