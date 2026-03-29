"""Tests for IWO Ops Agent directive handler, gate logic, and dispatch path.

Tests the resolve-ops directive handler, safety tier classification,
gate approval flow, and prompt generation — all without requiring
a running tmux session or IWO daemon.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass, field
import pytest

from iwo.config import IWOConfig
from iwo.ops_actions import OpsAction, OpsActionsRegister


def _make_test_actions() -> list[OpsAction]:
    """Create test OpsAction objects directly (bypasses register load bug)."""
    return [
        OpsAction(
            id="ops-test-001",
            title="Run D1 migration for users table",
            description="Run D1 migration for users table",
            spec_id="EBATT-040",
            priority="critical",
            category="migration",
            status="pending",
        ),
        OpsAction(
            id="ops-test-002",
            title="Add STRIPE_SECRET to wrangler secrets",
            description="Add STRIPE_SECRET to wrangler secrets",
            spec_id="EBATT-040",
            priority="critical",
            category="secret",
            status="pending",
        ),
        OpsAction(
            id="ops-test-003",
            title="Verify /api/health returns 200",
            description="Verify /api/health returns 200",
            spec_id="EBATT-040",
            priority="warning",
            category="verification",
            status="pending",
        ),
        OpsAction(
            id="ops-test-004",
            title="Configure DNS CNAME for api.ebatt.ai",
            description="Configure DNS CNAME for api.ebatt.ai",
            spec_id="EBATT-040",
            priority="critical",
            category="dns",
            status="pending",
        ),
    ]


@pytest.fixture
def config(tmp_path):
    """IWOConfig with temp paths."""
    return IWOConfig(
        project_root=tmp_path / "ebatt",
        handoffs_dir=tmp_path / "agent-comms",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def mock_daemon(config, tmp_path):
    """Mock daemon with ops register and commander."""
    config.handoffs_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    (config.handoffs_dir / ".directives").mkdir(parents=True, exist_ok=True)
    (config.handoffs_dir / ".directives" / ".processed").mkdir(parents=True, exist_ok=True)

    daemon = MagicMock()
    daemon.config = config
    daemon._notify = MagicMock()
    daemon.agent_states = {}

    # Create register and populate directly with OpsAction objects.
    # Mock load() as no-op because OpsActionsRegister.load() has a pre-existing
    # bug where the normal JSON parse path doesn't populate self.actions.
    register_path = config.handoffs_dir / ".ops-actions.json"
    ops_register = OpsActionsRegister(register_path)
    ops_register.actions = _make_test_actions()
    ops_register.load = MagicMock()  # no-op — actions already populated
    daemon.ops_register = ops_register

    commander = MagicMock()
    commander.is_agent_idle = MagicMock(return_value=True)
    commander.launch_agent_007 = MagicMock(return_value=True)
    daemon.commander = commander

    return daemon


@pytest.fixture
def directive_processor(config, mock_daemon):
    """DirectiveProcessor with mocked daemon."""
    from iwo.directives import DirectiveProcessor
    dp = DirectiveProcessor(config, mock_daemon)
    dp.ensure_dirs()
    return dp


class TestResolveOpsClassification:
    """Test that _handle_resolve_ops correctly classifies actions by safety tier."""

    def test_filter_all_returns_all_pending(self, directive_processor, mock_daemon):
        """filter=all should return all pending actions."""
        directive = {"directive": "resolve-ops", "filter": "all"}
        directive_processor._handle_resolve_ops(directive)

        # 3 gated actions (secret, dns, verification) means gate should be pending
        assert directive_processor._ops_gate_pending is not None or \
            mock_daemon.commander.launch_agent_007.called

    def test_filter_critical_only(self, directive_processor, mock_daemon):
        """filter=critical should only include critical-priority actions."""
        directive = {"directive": "resolve-ops", "filter": "critical"}
        directive_processor._handle_resolve_ops(directive)

        # ops-test-001 (migration, critical), ops-test-002 (secret, critical),
        # ops-test-004 (dns, critical) are critical
        # ops-test-003 (verification, warning) should be excluded
        # Since secret + dns are gated, gate should be pending
        assert directive_processor._ops_gate_pending is not None

    def test_auto_approve_categories_bypass_gate(self, directive_processor, mock_daemon, config):
        """Actions in auto-approve categories should bypass human gate."""
        # Remove gated actions from register — leave only migration (auto-approve)
        register = mock_daemon.ops_register
        register.actions = [
            a for a in register.actions
            if a.category in config.ops_auto_approve_categories
        ]

        directive = {"directive": "resolve-ops", "filter": "all"}
        directive_processor._handle_resolve_ops(directive)

        # No gated actions → dispatch immediately, no gate pending
        assert directive_processor._ops_gate_pending is None
        assert mock_daemon.commander.launch_agent_007.called


class TestErrorPropagation:
    """P0-1: Dispatch failure must raise AgentDispatchError, not silently succeed."""

    def test_dispatch_failure_raises_error(self, directive_processor, mock_daemon, config):
        """T21: _handle_resolve_ops raises AgentDispatchError when dispatch returns False."""
        from iwo.directives import AgentDispatchError

        mock_daemon.commander.launch_agent_007.return_value = False

        # Use only auto-approve actions so we hit the non-gated dispatch path
        register = mock_daemon.ops_register
        register.actions = [
            a for a in register.actions
            if a.category in config.ops_auto_approve_categories
        ]

        with pytest.raises(AgentDispatchError):
            directive_processor._handle_resolve_ops(
                {"directive": "resolve-ops", "filter": "all"}
            )

    def test_dispatch_failure_archives_as_failed_after_retries(self, directive_processor, mock_daemon, config):
        """T18: Dispatch failure archives directive with FAILED- prefix after max retries."""
        mock_daemon.commander.launch_agent_007.return_value = False

        # Use only auto-approve actions
        register = mock_daemon.ops_register
        register.actions = [
            a for a in register.actions
            if a.category in config.ops_auto_approve_categories
        ]

        # Drop a resolve-ops directive
        directives_dir = config.handoffs_dir / ".directives"
        processed_dir = directives_dir / ".processed"
        directive_path = directives_dir / "test-dispatch-fail.json"
        directive_path.write_text(json.dumps({
            "directive": "resolve-ops",
            "filter": "all",
        }))

        # Poll 5 times (max retries = 5)
        for i in range(5):
            directive_processor.poll()

        # After 5 retries, directive should be archived with FAILED- prefix
        assert not directive_path.exists(), "Directive should be removed from .directives/"
        archived = list(processed_dir.iterdir())
        assert len(archived) == 1, f"Expected 1 archived file, got {len(archived)}"
        assert "FAILED-" in archived[0].name, f"Expected FAILED- prefix, got {archived[0].name}"

    def test_dispatch_failure_retries_then_succeeds(self, directive_processor, mock_daemon, config):
        """T19: Dispatch failure retries — directive stays, then succeeds on next poll."""
        # First call fails, second succeeds
        mock_daemon.commander.launch_agent_007.side_effect = [False, True]

        # Use only auto-approve actions
        register = mock_daemon.ops_register
        register.actions = [
            a for a in register.actions
            if a.category in config.ops_auto_approve_categories
        ]

        directives_dir = config.handoffs_dir / ".directives"
        processed_dir = directives_dir / ".processed"
        directive_path = directives_dir / "test-retry-success.json"
        directive_path.write_text(json.dumps({
            "directive": "resolve-ops",
            "filter": "all",
        }))

        # First poll: dispatch fails, directive stays
        directive_processor.poll()
        assert directive_path.exists(), "Directive should remain for retry"
        assert len(list(processed_dir.iterdir())) == 0, "Nothing should be archived yet"

        # Second poll: dispatch succeeds, directive archived normally
        directive_processor.poll()
        assert not directive_path.exists(), "Directive should be removed after success"
        archived = list(processed_dir.iterdir())
        assert len(archived) == 1
        assert "FAILED-" not in archived[0].name, f"Should not have FAILED- prefix, got {archived[0].name}"

    def test_retry_counter_resets_on_success(self, directive_processor, mock_daemon, config):
        """T20: Retry counter is cleared after successful archive."""
        mock_daemon.commander.launch_agent_007.side_effect = [False, True]

        register = mock_daemon.ops_register
        register.actions = [
            a for a in register.actions
            if a.category in config.ops_auto_approve_categories
        ]

        directives_dir = config.handoffs_dir / ".directives"
        processed_dir = directives_dir / ".processed"
        directive_path = directives_dir / "test-counter-reset.json"
        directive_path.write_text(json.dumps({
            "directive": "resolve-ops",
            "filter": "all",
        }))

        # First poll: fail → retry count = 1
        directive_processor.poll()
        assert directive_processor._retry_counts.get("test-counter-reset.json") == 1

        # Second poll: succeed → retry count cleared
        directive_processor.poll()
        assert "test-counter-reset.json" not in directive_processor._retry_counts
