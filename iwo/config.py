"""IWO Configuration — Agent mapping, paths, thresholds."""

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

    # Agent → tmux window index mapping (used for discovery, not direct targeting)
    agent_window_map: dict[str, int] = field(default_factory=lambda: {
        "planner": 0,
        "builder": 1,
        "reviewer": 2,
        "tester": 3,
        "deployer": 4,
        "docs": 5,
    })

    # Safety rails
    max_rejection_loops: int = 5
    max_handoffs_per_spec: int = 150
    agent_timeout_seconds: int = 1800  # 30 minutes
    reconciliation_interval_seconds: int = 30

    # Agents that require human approval before IWO sends the command
    human_gate_agents: set[str] = field(default_factory=lambda: {"deployer"})

    # Debounce: seconds to wait after file creation before reading
    file_debounce_seconds: float = 1.5

    # The forced prompt set in agent shell sessions
    ready_prompt: str = "IWO_READY> "
    ready_prompt_pattern: str = r"IWO_READY> $"

    # Canary probe string
    canary_string: str = "# IWO_SYNC_CHECK"

    def __post_init__(self):
        if self.handoffs_dir is None:
            self.handoffs_dir = self.project_root / "docs" / "agent-comms"
        self.log_dir.mkdir(parents=True, exist_ok=True)
