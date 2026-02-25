"""IWO Configuration — Agent mapping, paths, thresholds.

Phase 3: Headless dispatch via HeadlessCommander.
Pane tagging, pipe-pane archival, reconciliation, deploy gate.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


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
    max_concurrent_specs: int = 5  # Phase 2.3: pipeline capacity limit

    # --- Agent Crash Recovery (Phase 2.4.1) ---
    max_respawn_attempts: int = 3  # per agent, before declaring permanently crashed
    respawn_cooldown_seconds: float = 30.0  # min seconds between respawn attempts

    # --- Post-Deploy Health Check (Phase 2.4.2) ---
    health_check_urls: list[str] = field(default_factory=lambda: [
        "https://ebatt.ai/api/health",
    ])
    health_check_timeout: int = 10  # seconds per URL
    health_check_expected_status: int = 200
    health_check_delay: float = 5.0  # seconds to wait after deploy before checking

    # --- Notification (Phase 2.5.2) ---
    notification_channels: list[str] = field(default_factory=lambda: ["ntfy"])  # "ntfy", "desktop", "webhook"
    notification_webhook_url: Optional[str] = None  # e.g., n8n webhook URL
    notification_webhook_timeout: int = 10  # seconds

    # ntfy push notifications (mobile) — https://ntfy.sh
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = "ebatt-ai"  # unique topic name — subscribe in ntfy app
    ntfy_timeout: int = 10  # seconds
    ntfy_priority_normal: int = 3  # ntfy: 1=min, 2=low, 3=default, 4=high, 5=urgent
    ntfy_priority_critical: int = 5  # used for deploy gates, failures, crashes

    # --- Self-Healing Ollama (Phase 3.0.4) ---
    ollama_auto_restart: bool = True  # attempt restart if Ollama unreachable
    ollama_restart_command: str = "systemctl --user start ollama"
    ollama_restart_max_attempts: int = 2
    ollama_restart_wait_seconds: float = 5.0  # wait after restart before retrying

    # --- Agent 007 (Phase 3) ---
    agent_007_window: int = 6
    agent_007_max_retries: int = 3
    agent_007_timeout_seconds: int = 600  # 10 min max runtime per activation
    agent_007_budget_usd: float = 5.0  # max API spend per activation

    # --- Pipeline staleness (Bug 3 fix) ---
    stale_pipeline_hours: float = 4.0  # pipelines with no handoff activity beyond this are stale
    agent_007_project_root: Path = Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt"

    # Agents that require human approval before IWO sends the command
    human_gate_agents: set[str] = field(default_factory=lambda: {"deployer"})

    # Auto-approve deploys when handoff declares no infrastructure changes
    # (noNewMigrations=true, noNewSecrets=true, noNewWranglerVars=true).
    # When False, ALL deploys require manual 'd' key approval.
    auto_approve_safe_deploys: bool = True

    # Auto-deploy: bypass the human gate entirely for ALL deploys, regardless
    # of infrastructure flags. Overrides auto_approve_safe_deploys.
    # Enable for overnight autonomous runs; disable for manual control.
    auto_deploy_all: bool = False

    # Auto-continue: when a pipeline completes (nextAgent=human, workflowComplete),
    # automatically queue a next-spec directive so Planner selects the next spec.
    # Enable for overnight autonomous runs; disable for manual control.
    auto_continue_on_completion: bool = False
    # Delay in seconds before issuing the auto-continue directive.
    # Gives agents time to settle and avoids dispatch during file writes.
    auto_continue_delay_seconds: float = 10.0

    # Debounce: seconds to wait after file creation before reading
    file_debounce_seconds: float = 1.5

    # --- State Polling (used by TUI timer) ---

    # Polling interval for agent state checks in the TUI
    state_poll_interval_seconds: float = 2.0

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
