"""IWO Agent State Machine — Phase 1.0.

Five states: IDLE, PROCESSING, STUCK, WAITING_HUMAN, CRASHED.
Deterministic transitions based on output observation, cursor position,
and pattern matching. No AI — just rules.

Design: Three-model consensus (Claude Opus 4.6, GPT-5.2, Gemini 3 Pro).
"""

import enum
import logging
import re
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .commander import AgentPane
    from .config import IWOConfig

log = logging.getLogger("iwo.state")


class AgentState(enum.Enum):
    """Agent pane states."""
    IDLE = "idle"                    # Prompt visible, output stable, cursor stationary
    PROCESSING = "processing"        # Output changing or cursor moving
    STUCK = "stuck"                  # No output for stuck_timeout without prompt
    WAITING_HUMAN = "waiting_human"  # Interactive prompt detected
    CRASHED = "crashed"              # Pane died or process exited
    UNKNOWN = "unknown"              # Initial state before first poll


class AgentStateMachine:
    """Tracks an agent's state via periodic polling.

    Call poll() every state_poll_interval_seconds. Reads output hash
    and cursor position from the AgentPane to determine transitions.
    """

    def __init__(self, pane: "AgentPane", config: "IWOConfig"):
        self.pane = pane
        self.config = config
        self.state = AgentState.UNKNOWN
        self._prev_state = AgentState.UNKNOWN

        # Output tracking
        self._last_output_hash: Optional[int] = None
        self._output_stable_since: float = 0.0  # timestamp when output stopped changing

        # Cursor tracking
        self._last_cursor: Optional[tuple[int, int]] = None
        self._cursor_stable_since: float = 0.0

        # Compile waiting-human patterns once
        self._human_patterns = [
            re.compile(p) for p in config.waiting_human_patterns
        ]
        self._idle_pattern = re.compile(config.idle_prompt_pattern)

    @property
    def agent_name(self) -> str:
        return self.pane.agent_name

    def poll(self) -> AgentState:
        """Check pane and update state. Returns current state."""
        now = time.time()

        # 1. Check if pane is alive
        if not self.pane.is_alive():
            return self._transition(AgentState.CRASHED)

        # 2. Capture output and cursor
        lines = self.pane.capture_visible(30)
        current_hash = hash(tuple(lines))
        cursor = self.pane.get_cursor_position()

        # 3. Track output stability
        output_changed = current_hash != self._last_output_hash
        if output_changed:
            self._last_output_hash = current_hash
            # Only reset stability timer if prompt is NOT visible.
            # Status bar redraws change the hash but shouldn't reset idle detection.
            if not self._check_idle_prompt(lines):
                self._output_stable_since = now
        elif self._output_stable_since == 0.0:
            self._output_stable_since = now

        # 4. Track cursor stability
        cursor_moved = cursor != self._last_cursor
        if cursor_moved:
            self._last_cursor = cursor
            if not self._check_idle_prompt(lines):
                self._cursor_stable_since = now
        elif self._cursor_stable_since == 0.0:
            self._cursor_stable_since = now

        # 5. Check for WAITING_HUMAN patterns
        if self._check_waiting_human(lines):
            return self._transition(AgentState.WAITING_HUMAN)

        # 6. If output or cursor is changing → PROCESSING
        #    UNLESS the idle prompt is visible — status bar redraws and cursor
        #    blinks cause hash/cursor changes even when the agent is genuinely
        #    idle. If prompt is visible, the agent is idle regardless of cosmetic
        #    output changes (Bug 1 fix: 2026-02-21).
        if output_changed or cursor_moved:
            if not self._check_idle_prompt(lines):
                return self._transition(AgentState.PROCESSING)
            # Prompt visible despite output/cursor change — cosmetic redraw.
            # Fall through to IDLE check below.

        # 7. Calculate stability duration
        output_stable_dur = now - self._output_stable_since
        cursor_stable_dur = now - self._cursor_stable_since

        # 8. Check for IDLE: prompt visible + stable output + stable cursor
        #    Fast path: if prompt is visible, require only 2s stability (not full
        #    output_stable_seconds). Prompt visibility is the strongest IDLE signal.
        prompt_visible = self._check_idle_prompt(lines)
        min_stable = 2.0 if prompt_visible else self.config.output_stable_seconds
        if (output_stable_dur >= min_stable
                and cursor_stable_dur >= min_stable
                and prompt_visible):
            return self._transition(AgentState.IDLE)

        # 9. Check for STUCK: stable but no prompt for too long
        if output_stable_dur >= self.config.stuck_timeout_seconds:
            return self._transition(AgentState.STUCK)

        # 10. Default: still processing (output stopped briefly but no prompt yet)
        if self.state == AgentState.UNKNOWN:
            return self._transition(AgentState.PROCESSING)

        return self.state

    def _check_idle_prompt(self, lines: list[str]) -> bool:
        """Check if any visible line matches the idle prompt pattern.

        Claude Code's prompt (❯) is NOT the last non-empty line — there's a
        status bar below it showing path and token count. So we search all
        lines from bottom up, stopping at the first match.
        """
        for line in reversed(lines):
            stripped = line.rstrip()
            if stripped and self._idle_pattern.search(stripped):
                return True
        return False

    def _check_waiting_human(self, lines: list[str]) -> bool:
        """Check last few lines for patterns that need human input."""
        # Only check last 5 lines for performance
        check_lines = lines[-5:] if len(lines) >= 5 else lines
        text = "\n".join(check_lines)
        return any(p.search(text) for p in self._human_patterns)

    def _transition(self, new_state: AgentState) -> AgentState:
        """Transition to a new state, logging changes."""
        if new_state != self.state:
            self._prev_state = self.state
            self.state = new_state
            log.info(
                f"[{self.agent_name}] {self._prev_state.value} → {new_state.value}"
            )
        return self.state

    def mark_command_sent(self):
        """Call after sending a command to force PROCESSING state."""
        self._output_stable_since = 0.0
        self._cursor_stable_since = 0.0
        self._last_output_hash = None
        self._last_cursor = None
        self._transition(AgentState.PROCESSING)
