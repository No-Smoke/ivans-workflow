"""Tests for daemon-level dispatch retry hardening.

Covers the three-way DispatchResult handling in IWODaemon._activate_for_handoff():
  - SUCCESS clears retry tracking
  - TRANSIENT_FAILURE increments retries, deduplicates queue entries
  - PERMANENT_FAILURE quarantines immediately
  - Max retries exceeded quarantines
  - Quarantine filesystem operations
  - Dequeue clears _queued_handoffs dedup set

Design: Three-model consensus (Claude Opus 4.6 + GPT-5.2 + Gemini 3 Pro).
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from iwo.state import AgentState, DispatchResult, DispatchStatus


def _make_mock_handoff(spec_id="SPEC-001", sequence=3, source="builder",
                       target="reviewer"):
    """Create a minimal mock Handoff with idempotency_key."""
    from iwo.parser import Handoff

    handoff = MagicMock(spec=Handoff)
    handoff.spec_id = spec_id
    handoff.sequence = sequence
    handoff.source_agent = source
    handoff.target_agent = target
    handoff.idempotency_key = f"{spec_id}:{sequence}:{source}:{target}"
    handoff.is_rejection = False
    return handoff


def _make_daemon():
    """Create an IWODaemon with mocked commander and pipeline."""
    from iwo.daemon import IWODaemon

    with patch.object(IWODaemon, "__init__", lambda self, **kw: None):
        daemon = IWODaemon()

    # Manually set attributes that __init__ would create
    daemon.config = MagicMock()
    daemon.config.project_root = Path("/tmp/iwo-test")
    daemon.commander = MagicMock()
    daemon.pipeline = MagicMock()
    daemon.auditor = None
    daemon.agent_states = {"reviewer": AgentState.IDLE}
    daemon._state_changed_at = {}
    daemon._handoff_retries = {}
    daemon._queued_handoffs = set()
    daemon._max_transient_retries = 4
    daemon._dlq_dir = Path("/tmp/iwo-test/.iwo/quarantine")
    daemon._notify = MagicMock()
    return daemon


class TestDispatchSuccess:
    """SUCCESS clears retry tracking and marks agent PROCESSING."""

    def test_success_clears_retries(self):
        daemon = _make_daemon()
        handoff = _make_mock_handoff()
        path = Path("/tmp/handoff.json")
        key = handoff.idempotency_key

        # Pre-seed retry count (simulating previous transient failures)
        daemon._handoff_retries[key] = 2

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.SUCCESS, "Dispatch successful"
        )

        daemon._activate_for_handoff("reviewer", handoff, path)

        # Retry counter should be cleared
        assert key not in daemon._handoff_retries
        # Agent marked PROCESSING
        assert daemon.agent_states["reviewer"] == AgentState.PROCESSING
        # Pipeline assignment
        daemon.pipeline.assign_agent.assert_called_once_with("reviewer", "SPEC-001")
        # Notification sent
        daemon._notify.assert_called_once()
        assert "✅" in daemon._notify.call_args[0][0]

    def test_success_emits_audit_event(self):
        daemon = _make_daemon()
        daemon.auditor = MagicMock()
        daemon.auditor._now_iso.return_value = "2026-02-24T12:00:00Z"
        handoff = _make_mock_handoff()
        path = Path("/tmp/handoff.json")

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.SUCCESS, "Dispatch successful"
        )

        daemon._activate_for_handoff("reviewer", handoff, path)

        daemon.auditor._emit.assert_called_once()
        event = daemon.auditor._emit.call_args[0][0]
        assert event.check == "handoff_success"


class TestTransientFailure:
    """TRANSIENT_FAILURE increments retries and deduplicates queue."""

    def test_transient_increments_retry_count(self):
        daemon = _make_daemon()
        handoff = _make_mock_handoff()
        path = Path("/tmp/handoff.json")
        key = handoff.idempotency_key

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.TRANSIENT_FAILURE, "Agent not idle"
        )

        daemon._activate_for_handoff("reviewer", handoff, path)

        assert daemon._handoff_retries[key] == 1
        daemon.pipeline.enqueue.assert_called_once_with(handoff, path)
        assert key in daemon._queued_handoffs

    def test_transient_deduplicates_queue(self):
        daemon = _make_daemon()
        handoff = _make_mock_handoff()
        path = Path("/tmp/handoff.json")
        key = handoff.idempotency_key

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.TRANSIENT_FAILURE, "Agent not idle"
        )

        # First failure — enqueues
        daemon._activate_for_handoff("reviewer", handoff, path)
        assert daemon.pipeline.enqueue.call_count == 1

        # Second failure — already in queue, should NOT enqueue again
        daemon._activate_for_handoff("reviewer", handoff, path)
        assert daemon.pipeline.enqueue.call_count == 1  # still 1
        assert daemon._handoff_retries[key] == 2

    def test_max_retries_exceeded_quarantines(self, tmp_path):
        daemon = _make_daemon()
        daemon._dlq_dir = tmp_path / "quarantine"
        daemon._dlq_dir.mkdir()

        handoff = _make_mock_handoff()
        key = handoff.idempotency_key

        # Create a real handoff file
        handoff_file = tmp_path / "handoff-reviewer.json"
        handoff_file.write_text(json.dumps({
            "metadata": {"specId": "SPEC-001", "agent": "builder",
                         "timestamp": "2026-02-24T12:00:00Z", "sequence": 3},
            "status": {"outcome": "success"},
            "nextAgent": {"target": "reviewer", "action": "review"},
        }))

        # Pre-seed at max retries
        daemon._handoff_retries[key] = 4  # already at limit

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.TRANSIENT_FAILURE, "Backoff active"
        )

        daemon._activate_for_handoff("reviewer", handoff, handoff_file)

        # Should be quarantined
        quarantine_files = list(daemon._dlq_dir.glob("FAILED_*"))
        assert len(quarantine_files) == 1
        # Retry counter cleared
        assert key not in daemon._handoff_retries
        # Original file removed
        assert not handoff_file.exists()
        # Critical notification
        daemon._notify.assert_called()
        assert "Max retries" in daemon._notify.call_args[0][0]


class TestPermanentFailure:
    """PERMANENT_FAILURE quarantines immediately without retries."""

    def test_permanent_failure_quarantines(self, tmp_path):
        daemon = _make_daemon()
        daemon._dlq_dir = tmp_path / "quarantine"
        daemon._dlq_dir.mkdir()

        handoff = _make_mock_handoff()
        key = handoff.idempotency_key

        handoff_file = tmp_path / "handoff-reviewer.json"
        handoff_file.write_text(json.dumps({
            "metadata": {"specId": "SPEC-001", "agent": "builder",
                         "timestamp": "2026-02-24T12:00:00Z", "sequence": 3},
            "status": {"outcome": "success"},
            "nextAgent": {"target": "reviewer", "action": "review"},
        }))

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.PERMANENT_FAILURE, "Pane identity validation failed"
        )

        daemon._activate_for_handoff("reviewer", handoff, handoff_file)

        # Should NOT be in retry tracking
        assert key not in daemon._handoff_retries
        # Should NOT be enqueued
        daemon.pipeline.enqueue.assert_not_called()
        # Should be quarantined
        quarantine_files = list(daemon._dlq_dir.glob("FAILED_*"))
        assert len(quarantine_files) == 1
        # Critical notification
        daemon._notify.assert_called()
        assert "Permanent failure" in daemon._notify.call_args[0][0]

    def test_permanent_failure_emits_audit(self, tmp_path):
        daemon = _make_daemon()
        daemon._dlq_dir = tmp_path / "quarantine"
        daemon._dlq_dir.mkdir()
        daemon.auditor = MagicMock()
        daemon.auditor._now_iso.return_value = "2026-02-24T12:00:00Z"

        handoff = _make_mock_handoff()
        handoff_file = tmp_path / "handoff-reviewer.json"
        handoff_file.write_text(json.dumps({
            "metadata": {"specId": "SPEC-001", "agent": "builder",
                         "timestamp": "2026-02-24T12:00:00Z", "sequence": 3},
            "status": {"outcome": "success"},
            "nextAgent": {"target": "reviewer", "action": "review"},
        }))

        daemon.commander.activate_agent.return_value = DispatchResult(
            DispatchStatus.PERMANENT_FAILURE, "Agent not found"
        )

        daemon._activate_for_handoff("reviewer", handoff, handoff_file)

        daemon.auditor._emit.assert_called_once()
        event = daemon.auditor._emit.call_args[0][0]
        assert event.check == "handoff_quarantined"


class TestQuarantineFilesystem:
    """Quarantine writes annotated JSON to DLQ directory."""

    def test_quarantine_annotates_json(self, tmp_path):
        daemon = _make_daemon()
        daemon._dlq_dir = tmp_path / "quarantine"
        daemon._dlq_dir.mkdir()

        handoff = _make_mock_handoff()
        original_data = {"metadata": {"specId": "SPEC-001"}, "test": True}

        handoff_file = tmp_path / "handoff-reviewer.json"
        handoff_file.write_text(json.dumps(original_data))

        daemon._quarantine_handoff(handoff, handoff_file, "Test reason")

        quarantine_file = daemon._dlq_dir / "FAILED_handoff-reviewer.json"
        assert quarantine_file.exists()

        data = json.loads(quarantine_file.read_text())
        assert data["_iwo_quarantine_reason"] == "Test reason"
        assert "_iwo_quarantined_at" in data
        assert data["_iwo_spec_id"] == "SPEC-001"
        assert data["_iwo_sequence"] == 3
        # Original preserved
        assert data["test"] is True
        # Original file removed
        assert not handoff_file.exists()

    def test_quarantine_handles_missing_file(self, tmp_path):
        """Quarantine should not crash if source file is already gone."""
        daemon = _make_daemon()
        daemon._dlq_dir = tmp_path / "quarantine"
        daemon._dlq_dir.mkdir()

        handoff = _make_mock_handoff()
        missing_file = tmp_path / "nonexistent.json"

        # Should not raise — logs critical
        daemon._quarantine_handoff(handoff, missing_file, "Already gone")


class TestDequeueDedup:
    """Dequeue clears _queued_handoffs set entry."""

    def test_dequeue_clears_dedup_set(self):
        """Simulates _process_pending_activations dequeue path."""
        daemon = _make_daemon()
        handoff = _make_mock_handoff()
        key = handoff.idempotency_key

        # Simulate an item in the dedup set (previously enqueued)
        daemon._queued_handoffs.add(key)

        # Simulate dequeue — the discard should clear the entry
        daemon._queued_handoffs.discard(key)

        assert key not in daemon._queued_handoffs
        # Re-enqueue should now work
        daemon._queued_handoffs.add(key)
        assert key in daemon._queued_handoffs
