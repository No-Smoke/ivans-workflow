"""IWO Configuration — Environment-driven, portable across machines.

All paths and service URLs are read from environment variables (IWO_*)
with sensible defaults. Create a .env file in the IWO repo root to
configure per-machine settings. See .env.example for documentation.

Phase 3: Headless dispatch via HeadlessCommander.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import os


def _iwo_root() -> Path:
    """Return the IWO repository root (directory containing this package)."""
    return Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load .env file from IWO repo root if python-dotenv is available."""
    env_file = _iwo_root() / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
    except ImportError:
        # python-dotenv not installed — parse manually (simple key=value)
        _load_dotenv_manual(env_file)


def _load_dotenv_manual(env_file: Path) -> None:
    """Minimal .env parser — no dependency required."""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:  # don't override existing env
            os.environ[key] = value


def _env(key: str, default: str = "") -> str:
    """Read an environment variable with fallback."""
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _env_int(key: str, default: int = 0) -> int:
    """Read an integer environment variable."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    """Read a float environment variable."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


# Load .env before dataclass defaults are evaluated
_load_dotenv()


@dataclass
class IWOConfig:
    """Central configuration for Ivan's Workflow Orchestrator.

    All settings can be overridden via IWO_* environment variables.
    Create a .env file in the repo root for per-machine config.
    """

    # ─── Paths ──────────────────────────────────────────────────
    # IWO_PROJECT_ROOT is required — no hardcoded default.
    project_root: Path = field(default_factory=lambda: Path(
        _env("IWO_PROJECT_ROOT", "")
    ) if _env("IWO_PROJECT_ROOT") else Path.cwd())

    handoffs_dir: Path = field(default=None)

    log_dir: Path = field(default_factory=lambda: Path(
        _env("IWO_LOG_DIR", str(_iwo_root() / "logs"))
    ))

    # Skills directory — bundled skills at {repo}/skills, overridable
    skills_dir: Path = field(default_factory=lambda: Path(
        _env("IWO_SKILLS_DIR", str(_iwo_root() / "skills"))
    ))

    # ─── tmux ────────────────────────────────────────────────────
    tmux_session_name: str = field(default_factory=lambda: _env(
        "IWO_TMUX_SESSION", "claude-agents"
    ))

    # Agent → tmux window index mapping
    agent_window_map: dict[str, int] = field(default_factory=lambda: {
        "planner": 0,
        "builder": 1,
        "reviewer": 2,
        "tester": 3,
        "deployer": 4,
        "docs": 5,
    })

    # Pane tagging — replaces window-index discovery
    pane_tag_key: str = "@iwo-agent"

    # ─── Safety Rails ───────────────────────────────────────────
    max_rejection_loops: int = 5
    max_handoffs_per_spec: int = 150
    agent_timeout_seconds: int = 1800  # 30 minutes
    max_concurrent_specs: int = 5

    # Agent Crash Recovery
    max_respawn_attempts: int = 3
    respawn_cooldown_seconds: float = 30.0

    # ─── Post-Deploy Health Check ──────────────────────────────
    health_check_urls: list[str] = field(default_factory=lambda: [
        url.strip() for url in _env("IWO_HEALTH_CHECK_URLS", "").split(",")
        if url.strip()
    ])
    health_check_timeout: int = 10
    health_check_expected_status: int = 200
    health_check_delay: float = 5.0

    # ─── Notifications ──────────────────────────────────────────
    notification_channels: list[str] = field(default_factory=lambda: ["ntfy"])
    notification_webhook_url: Optional[str] = field(default_factory=lambda: (
        _env("IWO_WEBHOOK_URL") or None
    ))
    notification_webhook_timeout: int = 10

    # ntfy push notifications
    ntfy_server: str = field(default_factory=lambda: _env(
        "IWO_NTFY_SERVER", "https://ntfy.sh"
    ))
    ntfy_topic: str = field(default_factory=lambda: _env("IWO_NTFY_TOPIC", ""))
    ntfy_timeout: int = 10
    ntfy_priority_normal: int = 3
    ntfy_priority_critical: int = 5

    # ─── Self-Healing Ollama ───────────────────────────────────
    ollama_auto_restart: bool = True
    ollama_restart_command: str = "systemctl --user start ollama"
    ollama_restart_max_attempts: int = 2
    ollama_restart_wait_seconds: float = 5.0

    # ─── Agent 007 ──────────────────────────────────────────────
    agent_007_window: int = 6
    agent_007_max_retries: int = 3
    agent_007_timeout_seconds: int = 600
    agent_007_budget_usd: float = 5.0

    # ─── Ops Actions ────────────────────────────────────────────
    ops_actions_notify_critical: bool = True
    ops_actions_notify_warning: bool = True
    ops_actions_daily_digest: bool = False
    ops_actions_daily_digest_hour: int = 8

    # Ops Agent (resolve-ops via Agent 007)
    ops_agent_enabled: bool = True
    ops_auto_approve_categories: set[str] = field(
        default_factory=lambda: {"migration", "config"}
    )
    ops_human_gate_categories: set[str] = field(
        default_factory=lambda: {"verification", "secret", "dns", "webhook", "email_infra", "other"}
    )
    ops_max_actions_per_run: int = 20
    ops_max_minutes_per_run: int = 10
    ops_proactive_threshold_minutes: int = 30

    # ─── Pipeline Staleness ───────────────────────────────────
    stale_pipeline_hours: float = 4.0

    # Agent 007 project root — defaults to same as project_root
    agent_007_project_root: Path = field(default=None)

    # ─── Deploy Gates ───────────────────────────────────────────
    human_gate_agents: set[str] = field(
        default_factory=lambda: {"deployer"}
    )
    auto_approve_safe_deploys: bool = field(default_factory=lambda: _env_bool(
        "IWO_AUTO_APPROVE_SAFE_DEPLOYS", True
    ))
    auto_deploy_all: bool = field(default_factory=lambda: _env_bool(
        "IWO_AUTO_DEPLOY_ALL", False
    ))
    auto_continue_on_completion: bool = field(default_factory=lambda: _env_bool(
        "IWO_AUTO_CONTINUE", False
    ))
    auto_continue_delay_seconds: float = 10.0

    # ─── Timing ─────────────────────────────────────────────────
    file_debounce_seconds: float = 1.5
    state_poll_interval_seconds: float = 2.0
    reconciliation_interval_seconds: int = 30
    enable_pipe_pane: bool = True

    # ─── Memory Integration ───────────────────────────────────
    enable_memory: bool = field(default_factory=lambda: _env_bool(
        "IWO_ENABLE_MEMORY", True
    ))
    qdrant_url: str = field(default_factory=lambda: _env("IWO_QDRANT_URL", ""))
    qdrant_api_key: str = field(default_factory=lambda: _env("IWO_QDRANT_API_KEY", ""))
    neo4j_uri: str = field(default_factory=lambda: _env("IWO_NEO4J_URI", ""))
    neo4j_user: str = field(default_factory=lambda: _env("IWO_NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("IWO_NEO4J_PASSWORD", ""))
    ollama_url: str = field(default_factory=lambda: _env(
        "IWO_OLLAMA_URL", "http://localhost:11434"
    ))
    ollama_embed_model: str = field(default_factory=lambda: _env(
        "IWO_OLLAMA_MODEL", "mxbai-embed-large"
    ))

    def __post_init__(self):
        # Derive handoffs_dir from project_root if not set
        if self.handoffs_dir is None:
            self.handoffs_dir = self.project_root / "docs" / "agent-comms"

        # Agent 007 defaults to same project root
        if self.agent_007_project_root is None:
            self.agent_007_project_root = self.project_root

        # Create log directory
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Disable memory if no endpoints configured
        if self.enable_memory and not self.qdrant_url and not self.neo4j_uri:
            self.enable_memory = False
