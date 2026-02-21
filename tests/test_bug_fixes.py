"""Tests for IWO bug fixes (2026-02-21).

Bug 1: State machine stuck in PROCESSING when prompt is visible
Bug 2: LATEST.json symlink management by HandoffHandler
Bug 3: Stale pipeline cleanup
Bug 5: Audit files filtered from HandoffHandler
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 1: State machine prompt-visible override ────────────────────


class TestStateMachineBug1:
    """State machine should detect IDLE when prompt is visible,
    even if output hash or cursor position changed (cosmetic redraws)."""

    def _make_sm(self):
        from iwo.state import AgentStateMachine, AgentState
        from iwo.config import IWOConfig

        config = IWOConfig()
        pane = MagicMock()
        pane.agent_name = "builder"
        pane.is_alive.return_value = True
        sm = AgentStateMachine(pane, config)
        return sm, pane

    def test_idle_when_prompt_visible_despite_output_change(self):
        """If prompt ❯ is visible, output changes should not force PROCESSING."""
        from iwo.state import AgentState

        sm, pane = self._make_sm()

        # First poll: establish baseline
        pane.capture_visible.return_value = ["some output", "❯ "]
        pane.get_cursor_position.return_value = (2, 10)
        sm.poll()

        # Wait for stability
        sm._output_stable_since = time.time() - 5
        sm._cursor_stable_since = time.time() - 5

        # Second poll: output hash changes (status bar update) but prompt still visible
        pane.capture_visible.return_value = ["some output", "tokens: 1234", "❯ "]
        pane.get_cursor_position.return_value = (2, 11)  # cursor moved slightly
        state = sm.poll()

        # Should be IDLE, not PROCESSING — prompt is visible
        assert state == AgentState.IDLE

    def test_processing_when_no_prompt_and_output_changes(self):
        """Without prompt visible, output changes should still mean PROCESSING."""
        from iwo.state import AgentState

        sm, pane = self._make_sm()

        # First poll
        pane.capture_visible.return_value = ["building file...", "compiling..."]
        pane.get_cursor_position.return_value = (0, 5)
        sm.poll()

        # Second poll: different output, no prompt
        pane.capture_visible.return_value = ["building file...", "still compiling..."]
        pane.get_cursor_position.return_value = (0, 6)
        state = sm.poll()

        assert state == AgentState.PROCESSING

    def test_fast_idle_with_prompt_visible(self):
        """IDLE transition should be fast (2s) when prompt is visible."""
        from iwo.state import AgentState

        sm, pane = self._make_sm()

        # Prompt visible, output stable for just 2.5 seconds
        pane.capture_visible.return_value = ["done.", "❯ "]
        pane.get_cursor_position.return_value = (2, 5)
        sm._last_output_hash = hash(tuple(["done.", "❯ "]))
        sm._last_cursor = (2, 5)
        sm._output_stable_since = time.time() - 2.5
        sm._cursor_stable_since = time.time() - 2.5

        state = sm.poll()
        assert state == AgentState.IDLE


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
