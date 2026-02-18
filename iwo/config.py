"""IWO Configuration — Agent mapping, paths, thresholds.

Phase 1.0: Added state machine config, pane tagging, canary probes,
waiting-human patterns, pipe-pane archival, reconciliation.
"""

from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class IWOConfig:
    """Central configuration for Ivan's Workflow Orchestrator."""

    # Paths
    project_root: Path = Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt"
    handoffs_dir: Path = field(default=None)
    log_dir: Path = Path.home() / "Nextcloud/PROJECTS/ivans-workflow-orchestrator/logs"

    # tmux
    tmux_session_name: str = "claude-agents"

    # Agent → tmux window index mapping (used for INITIAL tagging only in Phase 1)
    agent_window_map: dict[str, int] = field(default_factory=lambda: {
        "planner": 0,
        "builder": 1,
        "reviewer": 2,
        "tester": 3,
        "deployer": 4,
        "docs": 5,
    })

    # Pane tagging (Phase 1) — replaces window-index discovery
    pane_tag_key: str = "@iwo-agent"

    # Safety rails
    max_rejection_loops: int = 5
    max_handoffs_per_spec: int = 150
    agent_timeout_seconds: int = 1800  # 30 minutes

    # Agents that require human approval before IWO sends the command
    human_gate_agents: set[str] = field(default_factory=lambda: {"deployer"})

    # Debounce: seconds to wait after file creation before reading
    file_debounce_seconds: float = 1.5

    # --- State Machine (Phase 1) ---

    # Polling interval for agent state checks
    state_poll_interval_seconds: float = 2.0

    # How long output must be stable + cursor stationary to declare IDLE
    output_stable_seconds: float = 2.0

    # No output change for this long without prompt → STUCK
    stuck_timeout_seconds: float = 600.0  # 10 min: agents often wait legitimately

    # Claude Code's idle prompt pattern (cursor at end of this line = IDLE)
    # NOTE: IWO_READY> was the consensus design, but Claude Code manages its
    # own prompt. Default matches Claude Code's actual `> ` prompt.
    # Override if using a custom shell with PS1='IWO_READY> '.
    idle_prompt_pattern: str = r"[❯>]\s*$"  # Claude Code uses ❯, fallback matches >

    # Patterns indicating agent needs human input
    waiting_human_patterns: list[str] = field(default_factory=lambda: [
        r"\[Y/n\]",
        r"\[y/N\]",
        r"Password:",
        r"CONFLICT",
        r"--More--",
        r"Are you sure",
        r"Press ENTER",
        r"Continue\?",
        r"Overwrite",
        r"\(yes/no\)",
        r"Enter passphrase",
        r"Permission denied",
    ])

    # --- Canary Probe (Phase 1) ---

    canary_string: str = "# IWO_SYNC_CHECK"
    canary_timeout_seconds: float = 10.0
    canary_poll_interval_seconds: float = 0.5

    # --- Reconciliation (Phase 1) ---

    reconciliation_interval_seconds: int = 30

    # --- Pipe-pane archival (Phase 1) ---

    enable_pipe_pane: bool = True

    # --- Memory Integration (Phase 2.1) ---

    enable_memory: bool = True
    qdrant_url: str = "http://74.50.49.35:6333"
    qdrant_api_key: str = "qdrant-ethospower-2025-secure-key"
    neo4j_uri: str = "bolt://74.50.49.35:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "ebatt2025"
    ollama_url: str = "http://localhost:11434"
    ollama_embed_model: str = "mxbai-embed-large"

    def __post_init__(self):
        if self.handoffs_dir is None:
            self.handoffs_dir = self.project_root / "docs" / "agent-comms"
        self.log_dir.mkdir(parents=True, exist_ok=True)
