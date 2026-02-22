"""IWO TUI Dashboard — Phase 2.0.

Textual-based terminal UI showing live agent states, handoff history,
safety rail status, pending activations, and log output.

Replaces the headless daemon loop — drives polling via Textual timers
while watchdog runs in its background thread as before.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Container
from textual.reactive import reactive
from textual.widgets import Header, Footer, Static, RichLog, DataTable, Label
from textual.timer import Timer

from .config import IWOConfig
from .daemon import IWODaemon
from .state import AgentState
from .pipeline import SpecPipeline
from .metrics import MetricsCollector, PipelineMetrics

log = logging.getLogger("iwo.tui")


# ── State indicator symbols ──────────────────────────────────────────

STATE_DISPLAY = {
    AgentState.IDLE:          ("●", "green",   "IDLE"),
    AgentState.PROCESSING:    ("◉", "yellow",  "PROCESSING"),
    AgentState.STUCK:         ("⏳", "red",     "STUCK"),
    AgentState.WAITING_HUMAN: ("🙋", "magenta", "WAITING"),
    AgentState.CRASHED:       ("💀", "red",     "CRASHED"),
    AgentState.UNKNOWN:       ("○", "dim",     "UNKNOWN"),
}


# ── Widgets ──────────────────────────────────────────────────────────

class StatusBar(Static):
    """Top status bar: active pipelines, uptime, handoff count, queue depth."""

    specs_info = reactive("—")
    uptime = reactive("0s")
    handoff_count = reactive(0)
    queue_depth = reactive(0)

    def render(self) -> str:
        q_str = f" │ Queued: [yellow]{self.queue_depth}[/]" if self.queue_depth else ""
        return (
            f" Pipelines: [bold]{self.specs_info}[/] │ "
            f"Uptime: [cyan]{self.uptime}[/] │ "
            f"Handoffs: [yellow]{self.handoff_count}[/]"
            f"{q_str}"
        )


class AgentRow(Static):
    """Single agent status row."""

    agent_name: str = ""
    state = reactive(AgentState.UNKNOWN)
    state_age = reactive("")

    def __init__(self, agent_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.agent_name = agent_name

    def render(self) -> str:
        symbol, color, label = STATE_DISPLAY.get(
            self.state, ("?", "dim", "???")
        )
        name = self.agent_name.capitalize().ljust(10)
        label_str = label.ljust(12)
        return f" [{color}]{symbol}[/] {name} [{color}]{label_str}[/] {self.state_age}"


class AgentPanel(Container):
    """Panel showing all 6 agents and their states."""

    DEFAULT_CSS = """
    AgentPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    AgentPanel > Static.panel-title {
        text-style: bold;
        color: $text;
        padding: 0;
    }
    AgentRow {
        height: 1;
    }
    """

    def __init__(self, agent_names: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent_names = agent_names

    def compose(self) -> ComposeResult:
        yield Static("AGENTS", classes="panel-title")
        for name in self._agent_names:
            yield AgentRow(name, id=f"agent-{name}")


class PipelinePanel(Container):
    """Panel showing active spec pipelines and their status."""

    DEFAULT_CSS = """
    PipelinePanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    PipelinePanel > Static.panel-title {
        text-style: bold;
    }
    PipelinePanel > Static.pipeline-row {
        height: 1;
    }
    """

    MAX_ROWS = 6

    def compose(self) -> ComposeResult:
        yield Static("PIPELINES", classes="panel-title")
        for i in range(self.MAX_ROWS):
            yield Static("", id=f"pipeline-{i}", classes="pipeline-row")


class MemoryHealthPanel(Container):
    """Memory system health indicator: Qdrant, Neo4j, Ollama."""

    DEFAULT_CSS = """
    MemoryHealthPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    MemoryHealthPanel > Static.panel-title {
        text-style: bold;
    }
    MemoryHealthPanel > Static.memory-row {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("MEMORY", classes="panel-title")
        yield Static(" ○ Qdrant", id="mem-qdrant", classes="memory-row")
        yield Static(" ○ Neo4j", id="mem-neo4j", classes="memory-row")
        yield Static(" ○ Ollama", id="mem-ollama", classes="memory-row")


class SafetyPanel(Container):
    """Safety rails status display."""

    DEFAULT_CSS = """
    SafetyPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    SafetyPanel > Static.panel-title {
        text-style: bold;
    }
    SafetyPanel > Static.safety-row {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("SAFETY", classes="panel-title")
        yield Static("", id="safety-rejections", classes="safety-row")
        yield Static("", id="safety-handoffs", classes="safety-row")
        yield Static("", id="safety-deploy", classes="safety-row")
        yield Static("", id="safety-pending", classes="safety-row")


class HandoffPanel(Container):
    """Recent handoff history."""

    DEFAULT_CSS = """
    HandoffPanel {
        height: 100%;
        border: solid $accent;
        padding: 0 1;
    }
    HandoffPanel > Static.panel-title {
        text-style: bold;
    }
    HandoffPanel > Static.handoff-row {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("HANDOFF LOG", classes="panel-title")
        for i in range(12):
            yield Static("", id=f"handoff-{i}", classes="handoff-row")


class MetricsPanel(Container):
    """Pipeline performance metrics from Neo4j."""

    DEFAULT_CSS = """
    MetricsPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    MetricsPanel > Static.panel-title {
        text-style: bold;
    }
    MetricsPanel > Static.metrics-row {
        height: 1;
    }
    """

    MAX_AGENT_ROWS = 6

    def compose(self) -> ComposeResult:
        yield Static("METRICS", classes="panel-title")
        yield Static("", id="metrics-summary", classes="metrics-row")
        yield Static("", id="metrics-throughput", classes="metrics-row")
        yield Static("", id="metrics-bottleneck", classes="metrics-row")
        yield Static(" ─── Agent Cycle Times ───", classes="metrics-row")
        for i in range(self.MAX_AGENT_ROWS):
            yield Static("", id=f"metrics-agent-{i}", classes="metrics-row")


# ── Log Handler ──────────────────────────────────────────────────────

class TUILogHandler(logging.Handler):
    """Routes Python log records to a Textual RichLog widget."""

    def __init__(self, rich_log: RichLog):
        super().__init__()
        self.rich_log = rich_log

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.rich_log.write(msg)
        except Exception:
            pass


# ── Main App ─────────────────────────────────────────────────────────

class IWOApp(App):
    """Ivan's Workflow Orchestrator — TUI Dashboard."""

    TITLE = "IWO — Ivan's Workflow Orchestrator"
    SUB_TITLE = "Phase 2 Dashboard"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "deploy_approve", "Deploy Approve"),
        Binding("r", "force_reconcile", "Reconcile"),
        Binding("p", "pause_toggle", "Pause/Resume"),
    ]

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 3;
        grid-rows: 3 1fr 8;
        grid-columns: 1fr 1fr;
        grid-gutter: 0;
    }

    StatusBar {
        column-span: 2;
        height: 3;
        background: $boost;
        content-align: left middle;
        padding: 0 1;
        border-bottom: solid $accent;
        text-style: bold;
    }

    #left-col {
        height: 100%;
        layout: vertical;
    }

    #right-col {
        height: 100%;
    }

    #log-panel {
        column-span: 2;
        height: 8;
        border-top: solid $accent;
    }

    RichLog {
        height: 100%;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, config: Optional[IWOConfig] = None):
        super().__init__()
        self.config = config or IWOConfig()
        self.daemon = IWODaemon(self.config)
        self._paused = False
        self._poll_timer: Optional[Timer] = None
        self._recon_timer: Optional[Timer] = None
        self._display_timer: Optional[Timer] = None
        self._health_timer: Optional[Timer] = None
        self._metrics_timer: Optional[Timer] = None
        self._memory_health: dict[str, bool] = {"qdrant": False, "neo4j": False, "ollama": False}
        self._metrics: Optional[PipelineMetrics] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(id="status-bar")
        with Vertical(id="left-col"):
            yield AgentPanel(
                list(self.config.agent_window_map.keys()),
                id="agent-panel",
            )
            yield PipelinePanel(id="pipeline-panel")
            yield MemoryHealthPanel(id="memory-panel")
            yield SafetyPanel(id="safety-panel")
        with Vertical(id="right-col"):
            yield MetricsPanel(id="metrics-panel")
            yield HandoffPanel(id="handoff-panel")
        with Container(id="log-panel"):
            yield RichLog(
                highlight=True,
                markup=True,
                max_lines=200,
                auto_scroll=True,
                id="log-output",
            )
        yield Footer()

    def on_mount(self) -> None:
        """Start daemon and begin polling."""
        # Route all IWO logging to the RichLog widget
        rich_log = self.query_one("#log-output", RichLog)
        handler = TUILogHandler(rich_log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s │ %(message)s", datefmt="%H:%M:%S"))

        root_logger = logging.getLogger("iwo")
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        # Also capture watchdog logs
        wd_logger = logging.getLogger("watchdog")
        wd_logger.addHandler(handler)

        # Start daemon (connect, tag, init state machines, start watcher)
        rich_log.write("[bold green]Starting IWO daemon...[/]")
        if not self.daemon.setup():
            rich_log.write("[bold red]Failed to start daemon! Check tmux session.[/]")
            return

        rich_log.write(f"[bold green]IWO v1.0 ready — monitoring {len(self.daemon.commander.agents)} agents[/]")

        # Start polling timers
        poll_interval = self.config.state_poll_interval_seconds
        recon_interval = self.config.reconciliation_interval_seconds

        self._poll_timer = self.set_interval(poll_interval, self._poll_states)
        self._recon_timer = self.set_interval(recon_interval, self._reconcile)
        self._display_timer = self.set_interval(1.0, self._update_display)
        self._health_timer = self.set_interval(60.0, self._check_memory_health)
        self._metrics_timer = self.set_interval(60.0, self._refresh_metrics)

        # Run initial health check and metrics
        self._check_memory_health()
        self._refresh_metrics()

    def _poll_states(self) -> None:
        """Poll agent state machines."""
        if self._paused:
            return
        self.daemon._poll_agent_states()

    def _reconcile(self) -> None:
        """Run filesystem reconciliation."""
        if self._paused:
            return
        self.daemon._reconcile_filesystem()

    def _update_display(self) -> None:
        """Refresh all display widgets from daemon state."""
        self._update_status_bar()
        self._update_agents()
        self._update_pipelines()
        self._update_memory_health()
        self._update_metrics()
        self._update_safety()
        self._update_handoffs()

    def _check_memory_health(self) -> None:
        """Poll memory backends for connectivity. Called every 60s."""
        if self._paused:
            return
        if self.daemon.memory:
            self._memory_health = self.daemon.memory.health_check()
        else:
            self._memory_health = {"qdrant": False, "neo4j": False, "ollama": False}

    def _refresh_metrics(self) -> None:
        """Refresh pipeline metrics from Neo4j. Called every 60s."""
        if self._paused:
            return
        if self.daemon.metrics:
            self._metrics = self.daemon.metrics.collect()

    def _update_status_bar(self) -> None:
        status = self.query_one("#status-bar", StatusBar)

        # Pipeline summary
        pm = self.daemon.pipeline
        active = pm.active_count
        total = len(pm.all_pipelines)
        if total == 0:
            status.specs_info = "none"
        elif active == 1:
            # Show single spec name for backward-compat feel
            active_specs = [p for p in pm.all_pipelines if p.status == "active"]
            status.specs_info = active_specs[0].spec_id if active_specs else "none"
        else:
            status.specs_info = f"{active} active / {total} total"

        # Queue depth
        status.queue_depth = pm.total_queued()

        # Uptime
        elapsed = time.time() - self.daemon._started_at
        if elapsed < 60:
            status.uptime = f"{int(elapsed)}s"
        elif elapsed < 3600:
            status.uptime = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
        else:
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            status.uptime = f"{h}h {m}m"

        # Handoff count
        total = sum(self.daemon.tracker._spec_handoff_counts.values())
        status.handoff_count = total

    def _update_agents(self) -> None:
        now = time.time()
        for name, state in self.daemon.agent_states.items():
            try:
                row = self.query_one(f"#agent-{name}", AgentRow)
                row.state = state

                # Calculate time since last state change
                changed_at = self.daemon._state_changed_at.get(name, 0)
                if changed_at > 0:
                    age = now - changed_at
                    if age < 60:
                        row.state_age = f"{int(age)}s ago"
                    elif age < 3600:
                        row.state_age = f"{int(age // 60)}m ago"
                    else:
                        row.state_age = f"{int(age // 3600)}h ago"
                else:
                    row.state_age = ""
            except Exception:
                pass

    def _update_pipelines(self) -> None:
        """Update pipeline status panel."""
        pipelines = self.daemon.pipeline.all_pipelines
        status_icons = {
            "active": "🟢",
            "queued": "🟡",
            "halted": "🔴",
            "completed": "✅",
        }
        for i in range(PipelinePanel.MAX_ROWS):
            try:
                widget = self.query_one(f"#pipeline-{i}", Static)
                if i < len(pipelines):
                    p = pipelines[i]
                    icon = status_icons.get(p.status, "○")
                    agent = p.current_agent or "—"
                    count = f"({p.handoff_count})"
                    color = "green" if p.status == "active" else "dim"
                    widget.update(
                        f" {icon} [{color}]{p.spec_id:<20}[/] {agent:<10} {count}"
                    )
                else:
                    widget.update("")
            except Exception:
                pass

    def _update_memory_health(self) -> None:
        """Update memory health indicator panel."""
        for service in ("qdrant", "neo4j", "ollama"):
            up = self._memory_health.get(service, False)
            icon = "🟢" if up else "🔴"
            color = "green" if up else "red"
            label = service.capitalize()
            try:
                self.query_one(f"#mem-{service}", Static).update(
                    f" {icon} [{color}]{label}[/]"
                )
            except Exception:
                pass

    def _update_metrics(self) -> None:
        """Update pipeline metrics panel."""
        m = self._metrics
        if not m or m.total_handoffs == 0:
            try:
                self.query_one("#metrics-summary", Static).update(
                    " [dim]No handoff data yet[/]"
                )
                self.query_one("#metrics-throughput", Static).update("")
                self.query_one("#metrics-bottleneck", Static).update("")
                for i in range(MetricsPanel.MAX_AGENT_ROWS):
                    self.query_one(f"#metrics-agent-{i}", Static).update("")
            except Exception:
                pass
            return

        # Summary line
        rej_pct = round(m.total_rejections / m.total_handoffs * 100) if m.total_handoffs else 0
        rej_color = "red" if rej_pct > 20 else "yellow" if rej_pct > 10 else "green"
        try:
            self.query_one("#metrics-summary", Static).update(
                f" Handoffs: [cyan]{m.total_handoffs}[/] │ "
                f"Rejections: [{rej_color}]{m.total_rejections} ({rej_pct}%)[/]"
            )
        except Exception:
            pass

        # Throughput
        try:
            self.query_one("#metrics-throughput", Static).update(
                f" Throughput: [cyan]{m.handoffs_per_hour}/hr[/] (24h avg)"
            )
        except Exception:
            pass

        # Bottleneck
        try:
            if m.bottleneck_agent:
                self.query_one("#metrics-bottleneck", Static).update(
                    f" Bottleneck: [red]{m.bottleneck_agent}[/]"
                )
            else:
                self.query_one("#metrics-bottleneck", Static).update("")
        except Exception:
            pass

        # Per-agent rows (sorted by cycle time, slowest first)
        agents = sorted(m.agent_metrics, key=lambda a: -a.avg_cycle_minutes)
        for i in range(MetricsPanel.MAX_AGENT_ROWS):
            try:
                widget = self.query_one(f"#metrics-agent-{i}", Static)
                if i < len(agents):
                    a = agents[i]
                    name = a.agent.ljust(10)
                    cycle = f"{a.avg_cycle_minutes:.0f}m" if a.avg_cycle_minutes else "—"
                    rej = f"{a.rejection_rate:.0%}" if a.rejection_count else "0%"
                    r_color = "red" if a.rejection_rate > 0.2 else "dim"
                    widget.update(
                        f" {name} [cyan]{cycle:>5}[/] │ "
                        f"rej: [{r_color}]{rej}[/] │ n={a.handoff_count}"
                    )
                else:
                    widget.update("")
            except Exception:
                pass

    def _update_safety(self) -> None:
        tracker = self.daemon.tracker
        pm = self.daemon.pipeline

        # Rejection counts (show max across all pairs)
        max_rej = max(tracker._rejection_counts.values(), default=0)
        rej_color = "red" if max_rej >= self.config.max_rejection_loops - 1 else "green"
        try:
            self.query_one("#safety-rejections", Static).update(
                f" Rejections: [{rej_color}]{max_rej}/{self.config.max_rejection_loops}[/]"
            )
        except Exception:
            pass

        # Total handoffs across all active specs
        active_specs = [p for p in pm.all_pipelines if p.status == "active"]
        if active_specs:
            max_ho = max(
                tracker._spec_handoff_counts.get(p.spec_id, 0)
                for p in active_specs
            )
        else:
            max_ho = 0
        ho_color = "red" if max_ho >= self.config.max_handoffs_per_spec - 10 else "green"
        try:
            self.query_one("#safety-handoffs", Static).update(
                f" Max handoffs: [{ho_color}]{max_ho}/{self.config.max_handoffs_per_spec}[/]"
            )
        except Exception:
            pass

        # Deploy gate
        try:
            self.query_one("#safety-deploy", Static).update(
                " Deploy gate: [bold magenta]ACTIVE[/]"
            )
        except Exception:
            pass

        # Total queued activations across all agents
        total_queued = pm.total_queued()
        q_color = "yellow" if total_queued > 0 else "dim"
        try:
            self.query_one("#safety-pending", Static).update(
                f" Queued: [{q_color}]{total_queued}[/]"
            )
        except Exception:
            pass

    def _update_handoffs(self) -> None:
        history = self.daemon.handoff_history
        for i in range(12):
            try:
                widget = self.query_one(f"#handoff-{i}", Static)
                if i < len(history):
                    ho = history[i]
                    icon = "✅" if ho.status.outcome == "success" else "❌"
                    widget.update(
                        f" #{ho.sequence:>3} {ho.source_agent}→{ho.target_agent} {icon}"
                    )
                else:
                    widget.update("")
            except Exception:
                pass

    # ── Actions ──────────────────────────────────────────────────────

    def action_deploy_approve(self) -> None:
        """Manually approve deploy gate — dispatch the pending deploy handoff."""
        rich_log = self.query_one("#log-output", RichLog)
        pending = self.daemon._deploy_gate_pending
        if not pending:
            rich_log.write("[bold red]Deploy gate: no pending deploy to approve[/]")
            return

        handoff, path = pending
        rich_log.write(
            f"[bold yellow]Deploy gate: approving {handoff.spec_id}...[/]"
        )
        # Clear pending BEFORE dispatching to prevent double-approval
        self.daemon._deploy_gate_pending = None
        self.daemon._activate_for_handoff("deployer", handoff, path)
        rich_log.write("[bold green]Deploy gate: deployer activated![/]")

    def action_force_reconcile(self) -> None:
        """Force an immediate filesystem reconciliation."""
        rich_log = self.query_one("#log-output", RichLog)
        rich_log.write("[dim]Forcing reconciliation...[/]")
        self.daemon._reconcile_filesystem()

    def action_pause_toggle(self) -> None:
        """Pause/resume state polling and reconciliation."""
        self._paused = not self._paused
        rich_log = self.query_one("#log-output", RichLog)
        if self._paused:
            rich_log.write("[bold yellow]⏸ Polling PAUSED[/]")
        else:
            rich_log.write("[bold green]▶ Polling RESUMED[/]")

    def action_quit(self) -> None:
        """Clean shutdown."""
        self.daemon.shutdown()
        self.exit()


def main():
    """Entry point for iwo-tui command."""
    config = IWOConfig()
    if root := os.environ.get("IWO_PROJECT_ROOT"):
        config.project_root = Path(root)
        config.handoffs_dir = config.project_root / "docs" / "agent-comms"

    app = IWOApp(config)
    app.run()


if __name__ == "__main__":
    main()
