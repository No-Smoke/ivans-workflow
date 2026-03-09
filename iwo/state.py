"""IWO Agent States.

Defines the AgentState enum used throughout IWO for tracking agent status.
State is now derived from HeadlessCommander's deterministic idle detection
(pane_current_command ∈ {bash, zsh, sh, fish}) rather than the old
AgentStateMachine polling approach.

Note: AgentStateMachine was removed in Phase 3. State derivation lives
in daemon.py (_derive_agent_states).
"""

import enum
import logging

log = logging.getLogger("iwo.state")


class AgentState(enum.Enum):
    """Agent pane states."""
    IDLE = "idle"                    # Agent at shell prompt, ready for work
    PROCESSING = "processing"        # Claude subprocess running
    STUCK = "stuck"                  # No progress for extended period
    WAITING_HUMAN = "waiting_human"  # Interactive prompt detected
    CRASHED = "crashed"              # Pane died or process exited
    UNKNOWN = "unknown"              # Initial state before first poll
