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


def load_ops_actions(project_root: Path) -> dict:
    """Load ops actions register and compute summary counts."""
    ops_path = project_root / "docs" / "agent-comms" / ".ops-actions.json"
    result = {
        "actions": [],
        "pending_critical": 0,
        "pending_warning": 0,
        "pending_info": 0,
        "completed": 0,
        "skipped": 0,
        "total_pending": 0,
    }
    if not ops_path.exists():
        return result
    try:
        data = json.loads(ops_path.read_text())
        actions = data.get("actions", [])
        result["actions"] = actions
        for a in actions:
            status = a.get("status", "pending")
            priority = a.get("priority_override") or a.get("priority", "info")
            if status == "pending":
                if priority == "critical":
                    result["pending_critical"] += 1
                elif priority == "warning":
                    result["pending_warning"] += 1
                else:
                    result["pending_info"] += 1
                result["total_pending"] += 1
            elif status == "completed":
                result["completed"] += 1
            elif status == "skipped":
                result["skipped"] += 1
    except Exception:
        pass
    return result


def save_ops_actions(project_root: Path, actions_data: dict) -> bool:
    """Write ops actions register to disk with backup."""
    ops_path = project_root / "docs" / "agent-comms" / ".ops-actions.json"
    bak_path = ops_path.with_suffix(".json.bak")
    try:
        if ops_path.exists():
            import shutil
            shutil.copy2(ops_path, bak_path)
        actions_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        ops_path.write_text(json.dumps(actions_data, indent=2) + "\n")
        return True
    except Exception:
        return False


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
    """Determine the actively-building spec from ground truth (handoff files).

    Strategy: find the spec with the most recent non-terminal handoff.
    A terminal handoff has nextAgent.target in ('human', 'none') with
    workflowComplete=true. A non-terminal handoff means work is in progress.

    Fallback chain:
    1. Spec with most recent non-terminal LATEST.json (actively building)
    2. .active-specs.json — spec with current_agent assigned
    3. .current-spec file
    """
    # Strategy 1: scan LATEST.json across all spec dirs
    best_spec = None
    best_mtime = 0
    for d in agent_comms.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        latest = d / "LATEST.json"
        if not latest.exists():
            continue
        try:
            mtime = latest.stat().st_mtime
            data = json.loads(latest.read_text())
            next_target = data.get("nextAgent", {}).get("target", "")
            workflow_complete = data.get("status", {}).get("workflowComplete", False)
            # Skip terminal specs
            if workflow_complete or next_target in ("human", "none"):
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_spec = d.name
        except Exception:
            continue
    if best_spec:
        return best_spec

    # Strategy 2: .active-specs.json
    active_file = agent_comms / ".active-specs.json"
    if active_file.exists():
        try:
            data = json.loads(active_file.read_text())
            assignments = data.get("agent_assignments", {})
            for agent, spec_id in assignments.items():
                return spec_id
        except Exception:
            pass

    # Strategy 3: .current-spec
    f = agent_comms / ".current-spec"
    return f.read_text().strip() if f.exists() else ""



def build_html(project_root: Path) -> str:
    """Generate the full Kanban HTML dashboard."""
    agent_comms = project_root / "docs" / "agent-comms"
    current_spec = get_current_spec(agent_comms)
    pane_states = get_tmux_pane_states()
    specs = get_all_specs(agent_comms)
    ops = load_ops_actions(project_root)
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

    # Build banner text
    if ops['pending_critical'] > 0:
        banner_text = f"{ops['pending_critical']} critical ops action(s) pending"
        if ops['pending_warning'] > 0:
            banner_text += f" + {ops['pending_warning']} warning"
    elif ops['pending_warning'] > 0:
        banner_text = f"{ops['pending_warning']} warning ops action(s) pending"
    elif ops['total_pending'] > 0:
        banner_text = f"{ops['pending_info']} info ops action(s) pending"
    else:
        banner_text = "All ops actions resolved"

    # Ops banner/button colors
    ops_color = '#da3633' if ops['pending_critical'] > 0 else '#9e6a03' if ops['pending_warning'] > 0 else '#238636'
    ops_btn_label = f"Ops Actions ({ops['total_pending']})" if ops['total_pending'] > 0 else "Ops Actions"

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
header h1 {{ font-size: 18px; color: #58a6ff; font-weight: 600; }}
.header-right {{ display: flex; align-items: center; gap: 12px; }}
header .time {{ color: #8b949e; font-size: 13px; }}
.ops-btn {{
    display: inline-block; padding: 4px 12px; border-radius: 12px;
    font-size: 12px; font-weight: 600; text-decoration: none;
    color: #fff; background: {ops_color};
}}
.ops-btn:hover {{ opacity: 0.85; }}
.ops-banner {{
    padding: 8px 24px; font-size: 13px; font-weight: 600;
    text-align: center; background: {ops_color}; color: #fff;
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
    <div class="header-right">
        <a href="/ops" class="ops-btn">{ops_btn_label}</a>
        <span class="time">Updated {now} · Auto-refresh 5s</span>
    </div>
</header>
<div class="ops-banner">{banner_text}</div>
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



def _action_age(created_at: str) -> str:
    """Compute human-readable age from ISO timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h {(delta.seconds % 3600) // 60}m"
    except Exception:
        return "?"


def _escape(text: str) -> str:
    """Minimal HTML escaping."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_ops_html(project_root: Path) -> str:
    """Generate the Ops Actions register page at /ops."""
    ops = load_ops_actions(project_root)
    actions = ops["actions"]
    now = datetime.now().strftime("%H:%M:%S")

    # Group actions by status
    pending_critical = [a for a in actions if a["status"] == "pending" and (a.get("priority_override") or a["priority"]) == "critical"]
    pending_warning = [a for a in actions if a["status"] == "pending" and (a.get("priority_override") or a["priority"]) == "warning"]
    pending_info = [a for a in actions if a["status"] == "pending" and (a.get("priority_override") or a["priority"]) == "info"]
    completed = [a for a in actions if a["status"] == "completed"]
    skipped = [a for a in actions if a["status"] == "skipped"]

    def render_action(a: dict) -> str:
        priority = a.get("priority_override") or a["priority"]
        pri_colors = {"critical": "#da3633", "warning": "#9e6a03", "info": "#388bfd"}
        pri_color = pri_colors.get(priority, "#8b949e")
        age = _action_age(a.get("created_at", ""))
        stale_badge = ""
        if a.get("stale_since"):
            stale_badge = '<span style="background:#9e6a03;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;margin-left:8px;">POSSIBLY RESOLVED</span>'
        ver_cmd = ""
        if a.get("verification_cmd"):
            escaped_cmd = _escape(a["verification_cmd"])
            ver_cmd = f'<div class="ver-cmd" onclick="navigator.clipboard.writeText(this.innerText)" title="Click to copy"><code>{escaped_cmd}</code></div>'
        status_class = a["status"]
        action_id = _escape(a.get("id", ""))

        # Verification checklist for verification-category pending items
        verify_section = ""
        if a["status"] == "pending" and a.get("category") == "verification":
            verify_section = f'''<div class="verify-section" id="verify-{action_id}">
                <label class="verify-check"><input type="checkbox" id="chk-{action_id}"> Verified — mark as done</label>
                <textarea class="verify-comment" id="comment-{action_id}" rows="3" placeholder="What did you observe?"></textarea>
                <button class="btn-done" onclick="markDone('{action_id}')">Mark as Done</button>
                <span class="save-confirm" id="confirm-{action_id}"></span>
            </div>'''

        # Add note toggle for all pending items (non-verification get this as their interaction)
        note_section = ""
        if a["status"] == "pending" and a.get("category") != "verification":
            note_section = f'''<div class="note-toggle">
                <a href="#" class="note-link" onclick="toggleNote('{action_id}');return false;">Add note</a>
                <div class="note-box" id="note-{action_id}" style="display:none;">
                    <textarea class="verify-comment" id="notetext-{action_id}" rows="3" placeholder="Add observation or note..."></textarea>
                    <button class="btn-note" onclick="saveNote('{action_id}')">Save Note</button>
                    <span class="save-confirm" id="noteconfirm-{action_id}"></span>
                </div>
            </div>'''
        elif a["status"] == "pending" and a.get("category") == "verification":
            # Verification items also get note fallback if not completing
            pass  # verify_section already has a textarea

        # Show existing notes if present
        notes_display = ""
        if a.get("notes"):
            notes_display = f'<div class="ops-notes">Note: {_escape(a["notes"])}</div>'

        return f'''<div class="ops-action {status_class}" id="card-{action_id}">
            <div class="ops-action-header">
                <span class="pri-badge" style="background:{pri_color}">{priority.upper()}</span>
                <span class="ops-spec">{_escape(a.get("spec_id", ""))}</span>
                <span class="ops-id">{action_id}</span>
                <span class="ops-age">{age}</span>
                <span class="ops-cat">{_escape(a.get("category", ""))}</span>
                {stale_badge}
            </div>
            <div class="ops-title">{_escape(a.get("title", ""))}</div>
            <div class="ops-desc">{_escape(a.get("description", ""))}</div>
            {ver_cmd}
            {notes_display}
            {verify_section}
            {note_section}
        </div>'''

    def render_group(title: str, items: list[dict], collapse: bool = False) -> str:
        if not items:
            return ""
        cards = "\n".join(render_action(a) for a in items)
        open_attr = "" if collapse else " open"
        return f'<details{open_attr}><summary class="group-header">{title} ({len(items)})</summary><div class="group-body">{cards}</div></details>'

    groups_html = ""
    groups_html += render_group("Pending Critical", pending_critical)
    groups_html += render_group("Pending Warning", pending_warning)
    groups_html += render_group("Pending Info", pending_info)
    groups_html += render_group("Completed", completed, collapse=True)
    groups_html += render_group("Skipped", skipped, collapse=True)

    ops_color = '#da3633' if ops['pending_critical'] > 0 else '#9e6a03' if ops['pending_warning'] > 0 else '#238636'

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Ops Actions Register</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#0d1117; color:#c9d1d9; }}
header {{ background:#161b22; border-bottom:1px solid #30363d; padding:12px 24px; display:flex; justify-content:space-between; align-items:center; }}
header h1 {{ font-size:18px; color:#58a6ff; font-weight:600; }}
.header-right {{ display:flex; align-items:center; gap:12px; }}
header .time {{ color:#8b949e; font-size:13px; }}
a.back-btn {{ color:#58a6ff; text-decoration:none; font-size:13px; font-weight:600; }}
a.back-btn:hover {{ text-decoration:underline; }}
.summary-bar {{ background:#161b22; padding:12px 24px; border-bottom:1px solid #30363d; display:flex; gap:20px; align-items:center; }}
.summary-pill {{ padding:4px 12px; border-radius:12px; font-size:12px; font-weight:600; color:#fff; }}
.content {{ max-width:1000px; margin:0 auto; padding:20px 24px; }}
.group-header {{ font-size:14px; font-weight:700; color:#c9d1d9; padding:12px 0 8px; cursor:pointer; list-style:none; }}
.group-header::-webkit-details-marker {{ display:none; }}
.group-header::before {{ content:"\\25B6 "; font-size:10px; margin-right:6px; }}
details[open] > .group-header::before {{ content:"\\25BC "; }}
.group-body {{ display:flex; flex-direction:column; gap:8px; margin-bottom:16px; }}
.ops-action {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 16px; }}
.ops-action.completed {{ opacity:0.6; }}
.ops-action.skipped {{ opacity:0.5; }}
.ops-action-header {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }}
.pri-badge {{ padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; color:#fff; }}
.ops-spec {{ font-weight:600; color:#58a6ff; font-size:13px; }}
.ops-id {{ color:#484f58; font-size:11px; }}
.ops-age {{ color:#8b949e; font-size:11px; }}
.ops-cat {{ background:#21262d; padding:2px 6px; border-radius:4px; font-size:10px; color:#8b949e; }}
.ops-title {{ font-weight:600; font-size:14px; color:#c9d1d9; margin-bottom:4px; }}
.ops-desc {{ font-size:13px; color:#8b949e; line-height:1.4; margin-bottom:6px; }}
.ver-cmd {{ background:#0d1117; border:1px solid #21262d; border-radius:4px; padding:6px 10px; cursor:pointer; margin-top:4px; }}
.ver-cmd code {{ font-size:12px; color:#7ee787; word-break:break-all; }}
.ver-cmd:hover {{ border-color:#58a6ff; }}
.verify-section {{ margin-top:10px; padding-top:10px; border-top:1px solid #21262d; }}
.verify-check {{ display:flex; align-items:center; gap:8px; font-size:13px; color:#c9d1d9; cursor:pointer; margin-bottom:8px; }}
.verify-check input[type="checkbox"] {{ appearance:none; width:18px; height:18px; border:2px solid #30363d; border-radius:4px; background:#0d1117; cursor:pointer; position:relative; flex-shrink:0; }}
.verify-check input[type="checkbox"]:checked {{ background:#238636; border-color:#238636; }}
.verify-check input[type="checkbox"]:checked::after {{ content:"\\2713"; color:#fff; font-size:13px; position:absolute; top:0; left:3px; }}
.verify-comment {{ width:100%; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; font-size:13px; font-family:inherit; padding:8px; resize:vertical; margin-bottom:8px; }}
.verify-comment:focus {{ border-color:#58a6ff; outline:none; }}
.btn-done {{ background:#238636; color:#fff; border:none; border-radius:6px; padding:6px 16px; font-size:13px; font-weight:600; cursor:pointer; }}
.btn-done:hover {{ background:#2ea043; }}
.btn-done:disabled {{ opacity:0.5; cursor:not-allowed; }}
.btn-note {{ background:#1f6feb; color:#fff; border:none; border-radius:6px; padding:6px 16px; font-size:13px; font-weight:600; cursor:pointer; }}
.btn-note:hover {{ background:#388bfd; }}
.note-toggle {{ margin-top:8px; }}
.note-link {{ color:#58a6ff; font-size:12px; text-decoration:none; }}
.note-link:hover {{ text-decoration:underline; }}
.note-box {{ margin-top:8px; }}
.save-confirm {{ font-size:12px; color:#3fb950; margin-left:8px; opacity:0; transition:opacity 0.3s; }}
.save-confirm.show {{ opacity:1; }}
.ops-notes {{ font-size:12px; color:#8b949e; font-style:italic; margin-top:4px; padding:4px 8px; background:#0d1117; border-radius:4px; border-left:3px solid #30363d; }}
.ops-action.just-completed {{ border-color:#238636; }}
.ops-action.just-completed .ops-title::after {{ content:" \\2713"; color:#3fb950; }}
</style></head>
<body>
<header>
    <h1>Ops Actions Register</h1>
    <div class="header-right">
        <a href="/" class="back-btn">Back to Pipeline</a>
        <span class="time">Updated {now}</span>
    </div>
</header>
<div class="summary-bar">
    <span class="summary-pill" style="background:#da3633">{ops['pending_critical']} critical</span>
    <span class="summary-pill" style="background:#9e6a03">{ops['pending_warning']} warning</span>
    <span class="summary-pill" style="background:#388bfd">{ops['pending_info']} info</span>
    <span class="summary-pill" style="background:#238636">{ops['completed']} completed</span>
    <span class="summary-pill" style="background:#484f58">{ops['skipped']} skipped</span>
    <span style="color:#8b949e;font-size:12px;margin-left:auto;">{len(actions)} total actions</span>
</div>
<div class="content">
    {groups_html}
</div>
<script>
async function markDone(actionId) {{
    const chk = document.getElementById('chk-' + actionId);
    const comment = document.getElementById('comment-' + actionId);
    const confirm = document.getElementById('confirm-' + actionId);
    const btn = event.target;
    if (!chk.checked) {{
        comment.style.borderColor = '#da3633';
        setTimeout(() => comment.style.borderColor = '#30363d', 1500);
        return;
    }}
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {{
        const resp = await fetch('/api/ops/update', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                id: actionId,
                status: 'completed',
                resolved_by: 'vanya',
                notes: comment.value || 'Verified OK',
                resolved_at: new Date().toISOString()
            }})
        }});
        const data = await resp.json();
        if (data.ok) {{
            confirm.textContent = '\\u2713 Saved';
            confirm.classList.add('show');
            const card = document.getElementById('card-' + actionId);
            if (card) card.classList.add('just-completed');
            setTimeout(() => confirm.classList.remove('show'), 2000);
        }} else {{
            btn.textContent = 'Error: ' + (data.error || 'unknown');
        }}
    }} catch(e) {{
        btn.textContent = 'Network error';
    }}
}}
function toggleNote(actionId) {{
    const box = document.getElementById('note-' + actionId);
    box.style.display = box.style.display === 'none' ? 'block' : 'none';
}}
async function saveNote(actionId) {{
    const text = document.getElementById('notetext-' + actionId);
    const confirm = document.getElementById('noteconfirm-' + actionId);
    const btn = event.target;
    if (!text.value.trim()) return;
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {{
        const resp = await fetch('/api/ops/update', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ id: actionId, notes: text.value }})
        }});
        const data = await resp.json();
        if (data.ok) {{
            confirm.textContent = '\\u2713 Saved';
            confirm.classList.add('show');
            btn.textContent = 'Save Note';
            btn.disabled = false;
            setTimeout(() => confirm.classList.remove('show'), 2000);
        }} else {{
            btn.textContent = 'Error';
            setTimeout(() => {{ btn.textContent = 'Save Note'; btn.disabled = false; }}, 2000);
        }}
    }} catch(e) {{
        btn.textContent = 'Network error';
        setTimeout(() => {{ btn.textContent = 'Save Note'; btn.disabled = false; }}, 2000);
    }}
}}
</script>
</body></html>"""


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
        elif self.path == "/ops":
            html = build_ops_html(self.project_root)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/state":
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
        elif self.path == "/api/ops":
            ops = load_ops_actions(self.project_root)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ops, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/ops/update":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length))
                action_id = body.get("id")
                if not action_id:
                    self._json_response(400, {"ok": False, "error": "Missing 'id' field"})
                    return

                # Load current register
                ops_path = self.project_root / "docs" / "agent-comms" / ".ops-actions.json"
                if not ops_path.exists():
                    self._json_response(404, {"ok": False, "error": "Register not found"})
                    return
                data = json.loads(ops_path.read_text())
                actions = data.get("actions", [])

                # Find and update the action
                found = False
                PROTECTED_FIELDS = {"id", "fingerprint", "spec_id", "created_at", "auto_extracted"}
                ALLOWED_FIELDS = {"status", "resolved_by", "notes", "resolved_at"}
                for action in actions:
                    if action.get("id") == action_id:
                        for key in ALLOWED_FIELDS:
                            if key in body:
                                action[key] = body[key]
                        found = True
                        break

                if not found:
                    self._json_response(404, {"ok": False, "error": f"Action '{action_id}' not found"})
                    return

                if save_ops_actions(self.project_root, data):
                    self._json_response(200, {"ok": True, "id": action_id})
                else:
                    self._json_response(500, {"ok": False, "error": "Failed to write register"})
            except json.JSONDecodeError:
                self._json_response(400, {"ok": False, "error": "Invalid JSON"})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

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
