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
