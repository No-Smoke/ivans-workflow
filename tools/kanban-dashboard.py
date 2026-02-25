#!/usr/bin/env python3
"""IWF Kanban Dashboard — Live visual pipeline tracker.

Serves a browser-based Kanban board that reads IWF's agent-comms directory
and tmux pane state to show real-time pipeline progress.

Usage:
    python tools/kanban-dashboard.py [--port 8787] [--project-root /path/to/ebatt]

Opens http://localhost:8787 with auto-refresh every 5 seconds.
"""

import argparse
import json
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone


def get_project_root(override: str = None) -> Path:
    import os
    if override:
        return Path(override)
    root = os.environ.get("IWO_PROJECT_ROOT")
    if root:
        return Path(root)
    return Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt"


AGENTS_ORDERED = ["planner", "builder", "reviewer", "tester", "deployer", "docs"]



def get_tmux_pane_states() -> dict[str, dict]:
    """Query tmux for all agent pane states."""
    states = {}
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-t", "claude-agents",
             "-F", "#{window_index}:#{window_name}:#{pane_current_command}:#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return states
        for line in result.stdout.strip().splitlines():
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            idx, name, cmd, pid = parts
            # Derive agent name from window name (strip __ prefix and emoji)
            agent = name.lstrip("_").lower()
            # Strip leading emoji/non-ascii characters
            agent = "".join(c for c in agent if c.isascii()).strip()
            # Check for child processes
            has_children = False
            claude_running = False
            if pid:
                try:
                    child_result = subprocess.run(
                        ["pgrep", "-P", pid],
                        capture_output=True, text=True, timeout=3,
                    )
                    children = child_result.stdout.strip()
                    has_children = bool(children)
                    if has_children:
                        # Check if any child is claude
                        ps_result = subprocess.run(
                            ["ps", "-p", children.replace("\n", ","), "-o", "comm="],
                            capture_output=True, text=True, timeout=3,
                        )
                        claude_running = "claude" in ps_result.stdout.lower()
                except Exception:
                    pass
            if claude_running:
                status = "PROCESSING"
            elif has_children:
                status = "PROCESSING"
            elif cmd in ("bash", "zsh", "sh", "fish"):
                status = "IDLE"
            else:
                status = "UNKNOWN"
            states[agent] = {"cmd": cmd, "pid": pid, "status": status, "window": idx}
    except Exception:
        pass
    return states


def get_all_specs(agent_comms: Path) -> list[dict]:
    """Scan agent-comms for all spec directories and their handoffs."""
    specs = []
    if not agent_comms.exists():
        return specs
    for d in sorted(agent_comms.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        spec_id = d.name
        latest = d / "LATEST.json"
        handoffs = sorted([f for f in d.iterdir()
                          if f.suffix == ".json" and f.name != "LATEST.json"])
        spec_data = {
            "id": spec_id,
            "handoff_count": len(handoffs),
            "handoffs": [],
            "current_agent": None,
            "next_agent": None,
            "outcome": None,
            "summary": None,
        }
        # Parse each handoff for timeline
        for hf in handoffs:
            try:
                data = json.loads(hf.read_text())
                meta = data.get("metadata", {})
                status = data.get("status", {})
                next_ag = data.get("nextAgent", {})
                summ = data.get("summary", {})
                spec_data["handoffs"].append({
                    "file": hf.name,
                    "agent": meta.get("agent", "?"),
                    "sequence": meta.get("sequence", 0),
                    "timestamp": meta.get("timestamp", ""),
                    "outcome": status.get("outcome", "?"),
                    "target": next_ag.get("target", "?"),
                    "action": next_ag.get("action", "")[:80],
                    "one_liner": summ.get("oneLiner", "") if isinstance(summ, dict) else "",
                })
            except Exception:
                continue
        # Parse LATEST for current state
        if latest.exists():
            try:
                data = json.loads(latest.read_text())
                meta = data.get("metadata", {})
                status = data.get("status", {})
                next_ag = data.get("nextAgent", {})
                summ = data.get("summary", {})
                spec_data["current_agent"] = meta.get("agent")
                spec_data["next_agent"] = next_ag.get("target")
                spec_data["outcome"] = status.get("outcome")
                spec_data["summary"] = summ.get("oneLiner", "") if isinstance(summ, dict) else ""
            except Exception:
                pass
        specs.append(spec_data)
    return specs


def get_current_spec(agent_comms: Path) -> str:
    f = agent_comms / ".current-spec"
    return f.read_text().strip() if f.exists() else ""



def build_html(project_root: Path) -> str:
    """Generate the full Kanban HTML dashboard."""
    agent_comms = project_root / "docs" / "agent-comms"
    current_spec = get_current_spec(agent_comms)
    pane_states = get_tmux_pane_states()
    specs = get_all_specs(agent_comms)
    now = datetime.now().strftime("%H:%M:%S")

    # Find active spec data
    active_spec = None
    for s in specs:
        if s["id"] == current_spec:
            active_spec = s
            break

    # Build completed agents set for active spec — scoped to current sprint
    # A "sprint" starts at the most recent planner handoff
    completed_agents = set()
    active_agent = None
    pending_agents = []
    if active_spec:
        # Find the last planner handoff sequence to scope the current sprint
        last_planner_seq = 0
        for ho in active_spec["handoffs"]:
            if ho["agent"] == "planner":
                last_planner_seq = max(last_planner_seq, ho["sequence"])
        # Only count agents from the current sprint (>= last planner sequence)
        for ho in active_spec["handoffs"]:
            if ho["sequence"] >= last_planner_seq:
                completed_agents.add(ho["agent"])
        if active_spec["next_agent"] and active_spec["next_agent"] not in ("human", "none"):
            active_agent = active_spec["next_agent"]
        for ag in AGENTS_ORDERED:
            if ag in completed_agents:
                continue
            elif ag == active_agent:
                continue
            elif AGENTS_ORDERED.index(ag) > AGENTS_ORDERED.index(active_agent or "planner"):
                pending_agents.append(ag)

    # Build kanban columns
    columns_html = ""
    for ag in AGENTS_ORDERED:
        pane = pane_states.get(ag, {})
        pane_status = pane.get("status", "UNKNOWN")

        if ag in completed_agents:
            col_class = "completed"
            status_badge = '<span class="badge done">DONE</span>'
        elif ag == active_agent and pane_status == "PROCESSING":
            col_class = "active"
            status_badge = '<span class="badge processing">RUNNING</span>'
        elif ag == active_agent:
            col_class = "waiting"
            status_badge = '<span class="badge waiting">WAITING</span>'
        elif ag in pending_agents:
            col_class = "pending"
            status_badge = '<span class="badge pending">PENDING</span>'
        else:
            col_class = "pending"
            status_badge = '<span class="badge pending">PENDING</span>'

        # Find this agent's handoff details
        handoff_detail = ""
        if active_spec:
            for ho in active_spec["handoffs"]:
                if ho["agent"] == ag:
                    handoff_detail = f"""
                    <div class="handoff-detail">
                        <div class="outcome">{ho["outcome"]}</div>
                        <div class="action">{ho["one_liner"] or ho["action"]}</div>
                        <div class="timestamp">{ho["timestamp"]}</div>
                    </div>"""

        columns_html += f"""
        <div class="column {col_class}">
            <div class="column-header">
                <h3>{ag.upper()}</h3>
                {status_badge}
            </div>
            <div class="column-body">
                {handoff_detail if handoff_detail else '<div class="empty">—</div>'}
            </div>
            <div class="pane-info">tmux: {pane_status.lower()}</div>
        </div>"""

    # Build spec history sidebar
    history_html = ""
    for s in reversed(specs[-15:]):
        is_active = "active-spec" if s["id"] == current_spec else ""
        history_html += f"""
        <div class="spec-card {is_active}">
            <div class="spec-id">{s["id"]}</div>
            <div class="spec-summary">{s["summary"] or "—"}</div>
            <div class="spec-meta">{s["handoff_count"]} handoffs · {s["outcome"] or "—"}</div>
        </div>"""

    # Active spec card
    active_card = ""
    if active_spec:
        active_card = f"""
        <div class="active-spec-card">
            <h2>{active_spec["id"]}</h2>
            <p>{active_spec["summary"] or "No summary"}</p>
            <div class="progress-bar">
                <div class="progress-fill" style="width: {len(completed_agents) / len(AGENTS_ORDERED) * 100:.0f}%"></div>
            </div>
            <span class="progress-text">{len(completed_agents)}/{len(AGENTS_ORDERED)} stages complete</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>IWF Pipeline — {current_spec or "No Active Spec"}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    display: flex;
    flex-direction: column;
    height: 100vh;
}}
header {{
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 12px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
header h1 {{
    font-size: 18px;
    color: #58a6ff;
    font-weight: 600;
}}
header .time {{
    color: #8b949e;
    font-size: 13px;
}}
.main {{
    display: flex;
    flex: 1;
    overflow: hidden;
}}
.kanban {{
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 20px;
    gap: 16px;
}}
.active-spec-card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px 20px;
}}
.active-spec-card h2 {{
    color: #58a6ff;
    font-size: 20px;
    margin-bottom: 4px;
}}
.active-spec-card p {{
    color: #8b949e;
    font-size: 14px;
    margin-bottom: 12px;
}}
.progress-bar {{
    height: 6px;
    background: #21262d;
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 6px;
}}
.progress-fill {{
    height: 100%;
    background: #3fb950;
    border-radius: 3px;
    transition: width 0.5s ease;
}}
.progress-text {{
    font-size: 12px;
    color: #8b949e;
}}
.pipeline {{
    display: flex;
    gap: 12px;
    flex: 1;
    min-height: 0;
}}
.column {{
    flex: 1;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    min-width: 0;
}}
.column.completed {{ border-color: #3fb950; }}
.column.active {{ border-color: #58a6ff; box-shadow: 0 0 12px rgba(88,166,255,0.15); }}
.column.waiting {{ border-color: #d29922; }}
.column-header {{
    padding: 12px;
    border-bottom: 1px solid #21262d;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
.column-header h3 {{
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.5px;
    color: #c9d1d9;
}}
.badge {{
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
    text-transform: uppercase;
}}
.badge.done {{ background: #238636; color: #fff; }}
.badge.processing {{ background: #1f6feb; color: #fff; animation: pulse 2s infinite; }}
.badge.waiting {{ background: #9e6a03; color: #fff; }}
.badge.pending {{ background: #21262d; color: #8b949e; }}
@keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.6; }}
}}
.column-body {{
    flex: 1;
    padding: 12px;
    overflow-y: auto;
}}
.handoff-detail {{
    background: #0d1117;
    border-radius: 6px;
    padding: 10px;
}}
.handoff-detail .outcome {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    color: #3fb950;
    margin-bottom: 4px;
}}
.handoff-detail .action {{
    font-size: 13px;
    color: #c9d1d9;
    margin-bottom: 6px;
    line-height: 1.4;
}}
.handoff-detail .timestamp {{
    font-size: 11px;
    color: #484f58;
}}
.empty {{
    color: #484f58;
    font-size: 13px;
    text-align: center;
    padding: 20px 0;
}}
.pane-info {{
    padding: 6px 12px;
    border-top: 1px solid #21262d;
    font-size: 11px;
    color: #484f58;
}}
.sidebar {{
    width: 280px;
    background: #161b22;
    border-left: 1px solid #30363d;
    padding: 16px;
    overflow-y: auto;
}}
.sidebar h2 {{
    font-size: 13px;
    font-weight: 700;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 12px;
}}
.spec-card {{
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 10px;
    margin-bottom: 8px;
}}
.spec-card.active-spec {{
    border-color: #58a6ff;
}}
.spec-id {{
    font-size: 13px;
    font-weight: 600;
    color: #58a6ff;
    margin-bottom: 2px;
}}
.spec-summary {{
    font-size: 12px;
    color: #8b949e;
    margin-bottom: 4px;
    line-height: 1.3;
}}
.spec-meta {{
    font-size: 11px;
    color: #484f58;
}}
</style>
</head>
<body>
<header>
    <h1>Ivan's Workflow — Pipeline Dashboard</h1>
    <span class="time">Updated {now} · Auto-refresh 5s</span>
</header>
<div class="main">
    <div class="kanban">
        {active_card}
        <div class="pipeline">
            {columns_html}
        </div>
    </div>
    <div class="sidebar">
        <h2>Recent Specs</h2>
        {history_html}
    </div>
</div>
</body>
</html>"""



class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the Kanban dashboard."""

    project_root: Path = None  # Set by main()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = build_html(self.project_root)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/state":
            # JSON API for programmatic access
            agent_comms = self.project_root / "docs" / "agent-comms"
            data = {
                "current_spec": get_current_spec(agent_comms),
                "pane_states": get_tmux_pane_states(),
                "specs": get_all_specs(agent_comms),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def main():
    parser = argparse.ArgumentParser(description="IWF Kanban Dashboard")
    parser.add_argument("--port", type=int, default=8787, help="HTTP port (default: 8787)")
    parser.add_argument("--project-root", type=str, default=None,
                        help="eBatt project root path")
    args = parser.parse_args()

    project_root = get_project_root(args.project_root)
    if not (project_root / "docs" / "agent-comms").exists():
        print(f"ERROR: {project_root / 'docs/agent-comms'} not found", file=sys.stderr)
        sys.exit(1)

    DashboardHandler.project_root = project_root

    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"IWF Kanban Dashboard running at http://localhost:{args.port}")
    print(f"Project: {project_root}")
    print(f"Auto-refresh: 5 seconds")
    print("Press Ctrl+C to stop")

    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")
        server.server_close()


if __name__ == "__main__":
    main()
