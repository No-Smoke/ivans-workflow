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
    """Top status bar: spec name, uptime, handoff count."""

    spec = reactive("—")
    uptime = reactive("0s")
    handoff_count = reactive(0)

    def render(self) -> str:
        return (
            f" Spec: [bold]{self.spec}[/] │ "
            f"Uptime: [cyan]{self.uptime}[/] │ "
            f"Handoffs: [yellow]{self.handoff_count}[/]"
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(id="status-bar")
        with Vertical(id="left-col"):
            yield AgentPanel(
                list(self.config.agent_window_map.keys()),
                id="agent-panel",
            )
            yield SafetyPanel(id="safety-panel")
        with Vertical(id="right-col"):
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
        self._update_safety()
        self._update_handoffs()

    def _update_status_bar(self) -> None:
        status = self.query_one("#status-bar", StatusBar)

        # Current spec
        spec_file = self.config.handoffs_dir / ".current-spec"
        if spec_file.exists():
            try:
                status.spec = spec_file.read_text().strip()
            except Exception:
                status.spec = "—"
        else:
            status.spec = "—"

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
        for name, sm in self.daemon.state_machines.items():
            try:
                row = self.query_one(f"#agent-{name}", AgentRow)
                row.state = sm.state

                # Calculate time since output last changed
                if sm._output_stable_since > 0:
                    age = now - sm._output_stable_since
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

    def _update_safety(self) -> None:
        tracker = self.daemon.tracker

        # Rejection counts (show max across all pairs)
        max_rej = max(tracker._rejection_counts.values(), default=0)
        rej_color = "red" if max_rej >= self.config.max_rejection_loops - 1 else "green"
        try:
            self.query_one("#safety-rejections", Static).update(
                f" Rejections: [{rej_color}]{max_rej}/{self.config.max_rejection_loops}[/]"
            )
        except Exception:
            pass

        # Total handoffs for current spec
        spec_file = self.config.handoffs_dir / ".current-spec"
        spec_id = "—"
        if spec_file.exists():
            try:
                spec_id = spec_file.read_text().strip()
            except Exception:
                pass
        ho_count = tracker._spec_handoff_counts.get(spec_id, 0)
        ho_color = "red" if ho_count >= self.config.max_handoffs_per_spec - 10 else "green"
        try:
            self.query_one("#safety-handoffs", Static).update(
                f" Handoffs: [{ho_color}]{ho_count}/{self.config.max_handoffs_per_spec}[/]"
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

        # Pending activations
        pending = len(self.daemon._pending_activations)
        p_color = "yellow" if pending > 0 else "dim"
        try:
            self.query_one("#safety-pending", Static).update(
                f" Pending: [{p_color}]{pending}[/]"
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
        """Manually approve deploy gate — send /workflow-next to deployer."""
        deployer = self.daemon.commander.get_agent("deployer")
        rich_log = self.query_one("#log-output", RichLog)
        if deployer:
            rich_log.write("[bold yellow]Deploy gate: manually approving...[/]")
            success = self.daemon.commander.activate_agent("deployer")
            if success:
                sm = self.daemon.state_machines.get("deployer")
                if sm:
                    sm.mark_command_sent()
                rich_log.write("[bold green]Deploy gate: deployer activated![/]")
            else:
                rich_log.write("[bold red]Deploy gate: failed to activate deployer[/]")
        else:
            rich_log.write("[bold red]Deployer agent not found[/]")

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
