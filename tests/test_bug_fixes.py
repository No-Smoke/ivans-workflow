"""Tests for IWO bug fixes (2026-02-21 / 2026-02-23).

Bug 1: State machine stuck in PROCESSING when prompt is visible
Bug 2: LATEST.json symlink management by HandoffHandler
Bug 3: Stale pipeline cleanup
Bug 5: Audit files filtered from HandoffHandler
Bug 6: Interactive Claude sessions not detected as idle (2026-02-23)
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# ── Bug 3: Stale pipeline cleanup ───────────────────────────────────


class TestStalePipelineBug3:
    """Pipelines with no activity beyond threshold should be marked stale."""

    def test_release_stale_pipelines(self):
        from iwo.pipeline import PipelineManager

        pm = PipelineManager()
        pipeline = pm.get_or_create_pipeline("OLD-SPEC")
        pm.assign_agent("builder", "OLD-SPEC")
        # Make it old
        pipeline.last_handoff_at = time.time() - 20000  # ~5.5 hours ago
        pipeline.started_at = time.time() - 20000

        released = pm.release_stale_pipelines(14400)  # 4h threshold
        assert "OLD-SPEC" in released
        assert pipeline.status == "stale"
        assert pm.agent_current_spec("builder") is None

    def test_active_pipeline_not_released(self):
        from iwo.pipeline import PipelineManager

        pm = PipelineManager()
        pipeline = pm.get_or_create_pipeline("ACTIVE-SPEC")
        pm.assign_agent("builder", "ACTIVE-SPEC")
        pipeline.last_handoff_at = time.time() - 60  # 1 minute ago

        released = pm.release_stale_pipelines(14400)
        assert released == []
        assert pipeline.status == "active"
        assert pm.agent_current_spec("builder") == "ACTIVE-SPEC"

    def test_recover_stale_pipeline_no_agent_assignment(self):
        from iwo.pipeline import PipelineManager
        from iwo.parser import Handoff

        pm = PipelineManager()

        # Create a minimal handoff mock
        handoff = MagicMock(spec=Handoff)
        handoff.spec_id = "OLD-SPEC"
        handoff.target_agent = "builder"
        handoff.source_agent = "planner"
        handoff.is_rejection = False
        handoff.status = MagicMock()
        handoff.status.outcome = "success"

        # Recover with old mtime
        old_mtime = time.time() - 20000  # 5.5 hours ago
        pm.recover_from_handoffs("OLD-SPEC", [handoff],
                                  latest_mtime=old_mtime,
                                  stale_threshold_seconds=14400)

        pipeline = pm.get_pipeline("OLD-SPEC")
        assert pipeline.status == "stale"
        assert pm.agent_current_spec("builder") is None


# ── Bug 6: Interactive Claude sessions not detected as idle ────────


def _make_commander(agents_dict=None):
    """Build a HeadlessCommander with mocked config and agents."""
    from iwo.headless_commander import HeadlessCommander

    config = MagicMock()
    config.log_dir = Path("/tmp/iwo-test-logs")
    config.project_root = Path("/tmp/fake-project")
    config.tmux_session_name = "test"
    config.agent_window_map = {"builder": 1}
    config.pane_tag_key = "@iwo-agent"
    config.enable_pipe_pane = False

    hc = HeadlessCommander.__new__(HeadlessCommander)
    hc.config = config
    hc._server = MagicMock()
    hc._session = MagicMock()
    hc._agents = agents_dict or {}
    hc._active_agents = set()
    hc._session_ids = {}
    hc._prompt_dir = config.log_dir / "prompts"
    return hc


def _make_agent_pane(name, pane_current_command="bash", visible_lines=None):
    """Build a mock AgentPane with controllable pane state."""
    from iwo.commander import AgentPane

    mock_pane = MagicMock()
    type(mock_pane).pane_current_command = PropertyMock(
        return_value=pane_current_command
    )
    # capture-pane returns lines
    if visible_lines is not None:
        mock_pane.cmd.return_value = MagicMock(stdout=visible_lines)

    agent = AgentPane.__new__(AgentPane)
    agent.pane = mock_pane
    agent.agent_name = name
    agent.pane_id = f"%{name}"
    agent._last_command_time = 0.0
    return agent


class TestInteractiveIdleBug6:
    """Bug 6: HeadlessCommander should detect interactive Claude sessions
    at the `>` prompt as idle, not just bash/shell panes."""

    def test_bash_idle_detected(self):
        """pane_current_command=bash → idle (original behavior)."""
        agent = _make_agent_pane("builder", "bash")
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is True

    def test_claude_with_prompt_detected_idle(self):
        """pane_current_command=claude + `>` prompt visible → idle."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["some previous output", "", ">"]
        )
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is True

    def test_claude_with_partial_command_detected_idle(self):
        """pane_current_command=claude + `> /workflow-next` visible → idle."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["output line", "> /workflow-next"]
        )
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is True

    def test_claude_outputting_not_idle(self):
        """pane_current_command=claude + non-prompt text → busy."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["Reading file src/main.ts...", "Analyzing code..."]
        )
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is False

    def test_claude_active_agent_not_idle(self):
        """pane_current_command=claude + `>` prompt but tracked as active → busy."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=[">"]
        )
        hc = _make_commander({"builder": agent})
        hc._active_agents.add("builder")
        assert hc.is_agent_idle("builder") is False

    def test_unknown_command_not_idle(self):
        """pane_current_command=vim or other → not idle."""
        agent = _make_agent_pane("builder", "vim")
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is False

    def test_empty_pane_lines_not_idle(self):
        """pane_current_command=claude but only blank lines → not idle."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["", "", ""]
        )
        hc = _make_commander({"builder": agent})
        assert hc.is_agent_idle("builder") is False


class TestInteractiveDispatchBug6:
    """Bug 6: activate_agent() should route to interactive dispatch
    when pane has an interactive Claude session."""

    def test_dispatch_interactive_sends_workflow_next(self):
        """Interactive dispatch sends Ctrl+U then /workflow-next."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=[">"]
        )
        hc = _make_commander({"builder": agent})

        handoff = MagicMock()
        handoff.spec_id = "TEST-SPEC"
        handoff.sequence = 2
        handoff.source_agent = "planner"
        handoff.nextAgent = MagicMock()
        handoff.nextAgent.action = "Build the feature"

        result = hc.activate_agent("builder", handoff, Path("/tmp/fake.json"))

        assert result is True
        assert "builder" in hc._active_agents

        # Verify Ctrl+U was sent first, then /workflow-next
        calls = agent.pane.send_keys.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == "C-u"  # clear line
        assert calls[1][0][0] == "/workflow-next"  # dispatch command

    def test_dispatch_headless_when_bash(self):
        """Bash pane gets headless dispatch (original behavior)."""
        agent = _make_agent_pane("builder", "bash")
        hc = _make_commander({"builder": agent})

        handoff = MagicMock()
        handoff.spec_id = "TEST-SPEC"
        handoff.sequence = 3
        handoff.source_agent = "planner"
        handoff.nextAgent = MagicMock()
        handoff.nextAgent.action = "Build the feature"
        handoff.nextAgent.context = "Some context"

        handoff_path = Path("/tmp/fake-handoff.json")

        with patch.object(hc, "_dispatch_headless", return_value=True) as mock_hl:
            result = hc.activate_agent("builder", handoff, handoff_path)
            mock_hl.assert_called_once_with("builder", agent, handoff, handoff_path)
            assert result is True

    def test_completion_detection_interactive(self):
        """Active interactive agent returning to `>` prompt → completed."""
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["Task complete.", "", ">"]
        )
        hc = _make_commander({"builder": agent})
        hc._active_agents.add("builder")

        # Simulate: is_agent_idle returns True because `>` is visible
        # and agent will be removed from _active_agents when
        # check_completions runs. But wait — _active_agents check
        # in is_agent_idle will block this. We need to verify the
        # completion flow: check_completions calls is_agent_idle,
        # which checks _active_agents. For interactive mode,
        # the agent IS in _active_agents, so is_agent_idle returns False.
        #
        # This is correct! An active agent should NOT be detected as idle
        # until it's done. The issue is: how does completion actually work?
        #
        # For headless: pane_current_command goes from "claude" back to "bash"
        # For interactive: pane_current_command stays "claude", but `>`
        #   prompt reappears. However, the agent is in _active_agents,
        #   so is_agent_idle() returns False. This means interactive
        #   completion detection needs a different path.
        #
        # Actually, re-reading the code: is_agent_idle checks
        # `agent_name not in self._active_agents` for interactive mode.
        # So a tracked active agent will NOT be detected as idle.
        # For headless, the check is `cmd in IDLE_SHELLS` which doesn't
        # check _active_agents at all — so headless agents are detected
        # as complete when pane returns to bash, regardless.
        #
        # This means interactive completion needs to NOT check _active_agents.
        # Let me verify this is a real issue and fix it.
        pass  # This test exposed a bug — see fix below


class TestInteractiveCompletionBug6Fix:
    """Verify that check_completions works for interactive agents.

    The completion path: check_completions iterates _active_agents,
    calls is_agent_idle. For interactive mode, is_agent_idle must
    detect that the `>` prompt is back even though the agent is
    in _active_agents.

    Solution: For agents IN _active_agents, check_completions
    should use a separate completion check that doesn't filter
    by _active_agents membership.
    """

    def test_headless_completion(self):
        """Headless agent completes when pane returns to bash."""
        agent = _make_agent_pane("builder", "bash")
        hc = _make_commander({"builder": agent})
        hc._active_agents.add("builder")

        completed = hc.check_completions()
        assert "builder" in completed
        assert "builder" not in hc._active_agents

    def test_interactive_completion(self):
        """Interactive agent completes when `>` prompt reappears.

        This requires is_agent_idle to work for agents in _active_agents
        when called from check_completions.
        """
        agent = _make_agent_pane(
            "builder", "claude",
            visible_lines=["Done!", "", ">"]
        )
        hc = _make_commander({"builder": agent})
        hc._active_agents.add("builder")

        completed = hc.check_completions()
        # This SHOULD detect completion, but currently is_agent_idle
        # checks `agent_name not in self._active_agents` for interactive mode.
        # The fix: check_completions should temporarily remove the agent
        # from _active_agents for the idle check, or use a separate method.
        assert "builder" in completed
        assert "builder" not in hc._active_agents
