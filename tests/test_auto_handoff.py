"""Tests for iwo.auto_handoff — Phase 2.9.2 stall auto-recovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from iwo import auto_handoff
from iwo.auto_handoff import (
    AGENT_ORDER,
    _next_agent,
    generate_auto_handoff,
)


def test_next_agent_progression():
    assert _next_agent("planner") == "builder"
    assert _next_agent("builder") == "reviewer"
    assert _next_agent("reviewer") == "tester"
    assert _next_agent("tester") == "deployer"
    assert _next_agent("deployer") == "docs"
    assert _next_agent("docs") == "human"
    assert _next_agent("unknown-agent") == "human"


def test_agent_order_contract():
    assert AGENT_ORDER[0] == "planner"
    assert AGENT_ORDER[-1] == "docs"


@pytest.fixture
def patched_cmds():
    """Patch subprocess helpers to simulate a clean build."""
    with patch.object(
        auto_handoff,
        "_get_git_diff_stat",
        return_value=(["src/new.ts"], ["src/old.ts"]),
    ), patch.object(
        auto_handoff,
        "_run_tests",
        return_value={"passed": 10, "failed": 0, "skipped": 0},
    ), patch.object(
        auto_handoff, "_run_typecheck", return_value=True
    ):
        yield


def test_generate_auto_handoff_success(tmp_path: Path, patched_cmds):
    handoffs = tmp_path / "handoffs"
    result = generate_auto_handoff(
        agent_name="builder",
        spec_id="SPEC-001",
        last_sequence=2,
        project_dir=tmp_path,
        handoffs_dir=handoffs,
    )

    assert result is not None
    assert result.exists()
    assert result.parent == handoffs / "SPEC-001"
    assert result.name.startswith("003-builder-")
    assert result.name.endswith(".json")
    assert ":" not in result.name  # timestamp colons sanitized

    payload = json.loads(result.read_text())
    assert payload["metadata"]["specId"] == "SPEC-001"
    assert payload["metadata"]["agent"] == "builder"
    assert payload["metadata"]["sequence"] == 3
    assert payload["metadata"]["auto_generated"] is True
    assert payload["status"]["outcome"] == "success"
    assert payload["status"]["goalMet"] is True
    assert payload["nextAgent"]["target"] == "reviewer"
    assert payload["deliverables"]["filesCreated"] == ["src/new.ts"]
    assert payload["deliverables"]["filesModified"] == ["src/old.ts"]
    assert payload["deliverables"]["typecheckPassed"] is True


def test_generate_auto_handoff_tests_failing(tmp_path: Path):
    with patch.object(
        auto_handoff, "_get_git_diff_stat", return_value=([], [])
    ), patch.object(
        auto_handoff,
        "_run_tests",
        return_value={"passed": 5, "failed": 2, "skipped": 0},
    ), patch.object(
        auto_handoff, "_run_typecheck", return_value=True
    ):
        result = generate_auto_handoff(
            agent_name="tester",
            spec_id="SPEC-002",
            last_sequence=4,
            project_dir=tmp_path,
            handoffs_dir=tmp_path / "h",
        )

    assert result is not None
    payload = json.loads(result.read_text())
    assert payload["status"]["outcome"] == "partial"
    assert payload["status"]["goalMet"] is False
    assert "failures" in payload["status"]["notes"]
    assert payload["nextAgent"]["target"] == "deployer"


def test_generate_auto_handoff_typecheck_fail(tmp_path: Path):
    with patch.object(
        auto_handoff, "_get_git_diff_stat", return_value=([], [])
    ), patch.object(
        auto_handoff,
        "_run_tests",
        return_value={"passed": 3, "failed": 0, "skipped": 0},
    ), patch.object(
        auto_handoff, "_run_typecheck", return_value=False
    ):
        result = generate_auto_handoff(
            agent_name="builder",
            spec_id="SPEC-003",
            last_sequence=1,
            project_dir=tmp_path,
            handoffs_dir=tmp_path / "h",
        )

    payload = json.loads(result.read_text())
    assert payload["status"]["outcome"] == "partial"
    assert payload["deliverables"]["typecheckPassed"] is False
    assert "Typecheck" in payload["status"]["notes"]


def test_generate_auto_handoff_creates_spec_dir(tmp_path: Path, patched_cmds):
    handoffs = tmp_path / "nested" / "handoffs"
    result = generate_auto_handoff(
        agent_name="planner",
        spec_id="SPEC-XYZ",
        last_sequence=0,
        project_dir=tmp_path,
        handoffs_dir=handoffs,
    )
    assert result is not None
    assert (handoffs / "SPEC-XYZ").is_dir()
    assert result.name.startswith("001-planner-")


def test_run_cmd_timeout_returns_negative(tmp_path: Path):
    rc, out = auto_handoff._run_cmd(
        ["sleep", "5"], tmp_path, timeout=1
    )
    assert rc == -1
    assert out == "timeout"
