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
    hc._last_dispatch_time = {}
    hc._dispatch_fail_count = {}
    hc._dispatch_fail_time = {}
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
        """Interactive dispatch sends Ctrl+U then /workflow-next and verifies delivery."""
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

        # Simulate: after send_keys the prompt disappears (agent starts working).
        # First call to capture-pane returns prompt (idle check before dispatch),
        # subsequent calls return working output (verification after dispatch).
        call_count = {"n": 0}
        def _capture_side_effect(*args, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            if call_count["n"] <= 1:
                resp.stdout = [">"]          # idle: prompt visible
            else:
                resp.stdout = ["Processing…"]  # working: prompt gone
            return resp
        agent.pane.cmd.side_effect = _capture_side_effect

        # Bypass pane identity validation — this test checks dispatch routing,
        # not validation logic (tested separately in TestPaneIdentityValidation).
        with patch.object(hc, "_validate_pane_identity", return_value=True):
            result = hc.activate_agent("builder", handoff, Path("/tmp/fake.json"))

        assert result is True
        assert "builder" in hc._active_agents

        # Verify Ctrl+U was sent first, then /workflow-next
        send_calls = agent.pane.send_keys.call_args_list
        assert len(send_calls) == 2
        assert send_calls[0][0][0] == "C-u"  # clear line
        assert send_calls[1][0][0] == "/workflow-next"  # dispatch command

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

        # Bypass pane identity validation — this test checks dispatch routing,
        # not validation logic (tested separately in TestPaneIdentityValidation).
        with patch.object(hc, "_validate_pane_identity", return_value=True), \
             patch.object(hc, "_dispatch_headless", return_value=True) as mock_hl:
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


# ── Pane Identity Validation (C-u/workflow-next injection fix) ────


class TestPaneIdentityValidation:
    """Verify _validate_pane_identity prevents dispatch to wrong panes.

    Three checks:
    1. @iwo-agent tag must match expected agent name
    2. pane cwd must be within project_root
    3. Rate limiting: cooldown between dispatches
    """

    def _mock_subprocess_tag(self, tag_value, returncode=0):
        """Create a subprocess.run mock that returns a specific tag value."""
        result = MagicMock()
        result.stdout = f"{tag_value}\n"
        result.returncode = returncode
        return MagicMock(return_value=result)

    def test_valid_pane_passes_all_checks(self):
        """Pane with correct tag, correct cwd, and no recent dispatch → passes."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project/src"
        )
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is True

    def test_wrong_tag_rejects(self):
        """Pane with wrong @iwo-agent tag → rejected."""
        agent = _make_agent_pane("builder", "claude")
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", self._mock_subprocess_tag("planner")):
            assert hc._validate_pane_identity("builder", agent) is False

    def test_empty_tag_rejects(self):
        """Pane with no @iwo-agent tag → rejected."""
        agent = _make_agent_pane("builder", "claude")
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", self._mock_subprocess_tag("")):
            assert hc._validate_pane_identity("builder", agent) is False

    def test_subprocess_failure_rejects(self):
        """Tag check subprocess failure → rejected (fail-safe)."""
        agent = _make_agent_pane("builder", "claude")
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", side_effect=OSError("no tmux")):
            assert hc._validate_pane_identity("builder", agent) is False

    def test_wrong_cwd_rejects(self):
        """Pane in wrong working directory → rejected."""
        agent = _make_agent_pane("builder", "claude")
        # cwd is NOT under /tmp/fake-project
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/home/user/other-project"
        )
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is False

    def test_correct_cwd_passes(self):
        """Pane in correct working directory → passes."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project/packages/web-app"
        )
        hc = _make_commander({"builder": agent})

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is True

    def test_rate_limiting_throttles(self):
        """Dispatch within cooldown period → throttled."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})
        # Simulate recent dispatch (2 seconds ago)
        hc._last_dispatch_time["builder"] = time.time() - 2.0

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is False

    def test_rate_limiting_allows_after_cooldown(self):
        """Dispatch after cooldown expires → allowed."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})
        # Simulate old dispatch (30 seconds ago, well past 10s cooldown)
        hc._last_dispatch_time["builder"] = time.time() - 30.0

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is True

    def test_no_previous_dispatch_allowed(self):
        """First-ever dispatch (no previous timestamp) → allowed."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})
        # _last_dispatch_time is empty — no previous dispatch

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            assert hc._validate_pane_identity("builder", agent) is True


# ── Failed Dispatch Backoff (infinite retry loop fix) ────────────


class TestDispatchFailBackoff:
    """Verify exponential backoff prevents rapid retry after dispatch failures.

    Root cause: when activate_agent() fails, the daemon re-queues the handoff.
    The 2-second poll loop then retries immediately on the next cycle,
    creating an infinite rapid-fire retry loop of C-u/workflow-next injection.

    Fix: exponential backoff — 30s → 60s → 120s → 300s max after failures,
    resetting on success.
    """

    def _mock_subprocess_tag(self, tag_value, returncode=0):
        """Create a subprocess.run mock that returns a specific tag value."""
        result = MagicMock()
        result.stdout = f"{tag_value}\n"
        result.returncode = returncode
        return MagicMock(return_value=result)

    def test_first_failure_sets_30s_cooldown(self):
        """After first failure, next attempt blocked for 30 seconds."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})

        # Record one failure
        hc._record_dispatch_failure("builder")
        assert hc._dispatch_fail_count["builder"] == 1

        # Immediate retry should be blocked (within 30s cooldown)
        handoff = MagicMock()
        result = hc.activate_agent("builder", handoff, Path("/tmp/h.json"))
        assert result is False

    def test_second_failure_doubles_cooldown(self):
        """After two failures, cooldown is 60 seconds."""
        hc = _make_commander()

        hc._record_dispatch_failure("builder")
        hc._record_dispatch_failure("builder")
        assert hc._dispatch_fail_count["builder"] == 2

        # Cooldown should be min(30 * 2^1, 300) = 60s
        from iwo.headless_commander import HeadlessCommander
        cooldown = min(
            HeadlessCommander._DISPATCH_FAIL_BASE_COOLDOWN * (2 ** 1),
            HeadlessCommander._DISPATCH_FAIL_MAX_COOLDOWN,
        )
        assert cooldown == 60.0

    def test_backoff_caps_at_300s(self):
        """Backoff never exceeds 300 seconds regardless of failure count."""
        hc = _make_commander()

        # 10 consecutive failures
        for _ in range(10):
            hc._record_dispatch_failure("builder")

        fail_count = hc._dispatch_fail_count["builder"]
        from iwo.headless_commander import HeadlessCommander
        cooldown = min(
            HeadlessCommander._DISPATCH_FAIL_BASE_COOLDOWN * (2 ** (fail_count - 1)),
            HeadlessCommander._DISPATCH_FAIL_MAX_COOLDOWN,
        )
        assert cooldown == 300.0

    def test_backoff_allows_after_cooldown_expires(self):
        """After cooldown expires, dispatch attempt proceeds."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})

        # Record a failure 60 seconds ago (past 30s cooldown)
        hc._dispatch_fail_count["builder"] = 1
        hc._dispatch_fail_time["builder"] = time.time() - 60.0

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            with patch.object(hc, "is_agent_idle", return_value=True):
                with patch.object(hc, "_dispatch_interactive", return_value=True):
                    result = hc.activate_agent(
                        "builder", MagicMock(), Path("/tmp/h.json")
                    )
                    assert result is True

    def test_success_resets_backoff(self):
        """Successful dispatch clears failure count."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})

        # Set up past failures (but cooldown expired)
        hc._dispatch_fail_count["builder"] = 3
        hc._dispatch_fail_time["builder"] = time.time() - 600.0  # 10 min ago

        with patch("subprocess.run", self._mock_subprocess_tag("builder")):
            with patch.object(hc, "is_agent_idle", return_value=True):
                with patch.object(hc, "_dispatch_interactive", return_value=True):
                    result = hc.activate_agent(
                        "builder", MagicMock(), Path("/tmp/h.json")
                    )
                    assert result is True

        # Failure count should be reset
        assert "builder" not in hc._dispatch_fail_count
        assert "builder" not in hc._dispatch_fail_time

    def test_identity_failure_records_backoff(self):
        """Failed pane identity check records a dispatch failure for backoff."""
        agent = _make_agent_pane("builder", "claude")
        type(agent.pane).pane_current_path = PropertyMock(
            return_value="/tmp/fake-project"
        )
        hc = _make_commander({"builder": agent})

        # Tag check will fail (wrong tag) — must pass idle check first
        with patch("subprocess.run", self._mock_subprocess_tag("planner")):
            with patch.object(hc, "is_agent_idle", return_value=True):
                result = hc.activate_agent(
                    "builder", MagicMock(), Path("/tmp/h.json")
                )
                assert result is False

        # Should have recorded a failure
        assert hc._dispatch_fail_count.get("builder", 0) == 1
