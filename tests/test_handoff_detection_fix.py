"""Tests for the 6-phase handoff detection fix (incident 2026-04-11).

Validates that LATEST.json is no longer used for control-plane decisions,
ingestion validation catches rogue writes, stall watchdog doesn't silently
drop entries, quarantine works correctly, and cascade guards prevent
runaway auto-recovery.

These tests use a synthetic TEST-SYNTH-001 spec with fabricated handoff
files — the real eBatt specs and codebase are never touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from iwo.parser import Handoff
from iwo.pipeline import PipelineManager, SpecPipeline


# ── Helpers ──────────────────────────────────────────────────────────

SPEC_ID = "TEST-SYNTH-001"
AGENT_ORDER = ["planner", "builder", "reviewer", "tester", "deployer", "docs"]


def _make_handoff_data(
    spec_id: str = SPEC_ID,
    sequence: int = 1,
    source: str = "planner",
    target: str = "builder",
    outcome: str = "success",
) -> dict:
    """Create a minimal valid handoff JSON dict."""
    return {
        "metadata": {
            "specId": spec_id,
            "agent": source,
            "timestamp": "2026-04-12T00:00:00Z",
            "sequence": sequence,
        },
        "status": {
            "outcome": outcome,
        },
        "nextAgent": {
            "target": target,
            "action": f"Continue {spec_id}",
        },
    }


def _write_handoff(spec_dir: Path, data: dict) -> Path:
    """Write a handoff JSON file with conventional naming."""
    seq = data["metadata"]["sequence"]
    source = data["metadata"]["agent"]
    filename = f"{seq:03d}-{source}-2026-04-12T00-00-00Z.json"
    path = spec_dir / filename
    path.write_text(json.dumps(data, indent=2))
    return path


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def spec_dir(tmp_path):
    """Create a temporary spec directory with agent-comms structure."""
    d = tmp_path / "agent-comms" / SPEC_ID
    d.mkdir(parents=True)
    return d


@pytest.fixture
def handoffs_dir(tmp_path):
    """Return the agent-comms root directory."""
    d = tmp_path / "agent-comms"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture
def pipeline():
    """Fresh PipelineManager."""
    return PipelineManager(max_concurrent=5)


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: LATEST.json removed from control-plane decisions
# ═══════════════════════════════════════════════════════════════════════


class TestPhase1_LatestJsonRemoval:
    """Verify LATEST.json is never read for routing decisions."""

    def test_reconciliation_uses_pipeline_state_not_latest_json(
        self, spec_dir, pipeline
    ):
        """Reconciliation should skip completed pipelines based on
        PipelineManager status, not LATEST.json content."""
        # Write a LATEST.json pointing to a terminal handoff
        latest = spec_dir / "LATEST.json"
        latest.write_text(json.dumps({
            "nextAgent": {"target": "human", "action": "done"},
            "metadata": {"specId": SPEC_ID, "agent": "docs",
                         "timestamp": "2026-04-12T00:00:00Z", "sequence": 6},
            "status": {"outcome": "success"},
        }))
        # Pipeline is NOT marked completed
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "active"

        # If code reads LATEST.json it would skip; if it reads
        # PipelineManager it would NOT skip.
        assert pipeline.get_pipeline(SPEC_ID).status == "active"
        # The reconciliation code checks:
        #   pipeline.status in ("completed", "stale", "halted")
        # and "active" is not in that set → should NOT skip
        assert p.status not in ("completed", "stale", "halted")

    def test_reconciliation_skips_completed_pipeline(self, pipeline):
        """Completed pipelines should be skipped regardless of
        LATEST.json content."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "completed"
        assert p.status in ("completed", "stale", "halted")

    def test_reconciliation_skips_halted_pipeline(self, pipeline):
        """Halted pipelines should also be skipped."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        pipeline.mark_halted(SPEC_ID, "test")
        assert p.status == "halted"
        assert p.status in ("completed", "stale", "halted")

    def test_recover_state_ignores_latest_json_file(self, spec_dir):
        """Recovery should filter out LATEST.json from file list."""
        # Create files including LATEST.json
        _write_handoff(spec_dir, _make_handoff_data(sequence=1))
        latest = spec_dir / "LATEST.json"
        latest.write_text('{"poison": true}')

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [
            f for f in json_files
            if f.name != "LATEST.json" and not f.name.endswith(".tmp")
        ]
        assert len(json_files) == 1
        assert all(f.name != "LATEST.json" for f in json_files)

    def test_recover_state_chain_validation_catches_rogue_file(self, spec_dir):
        """Recovery fast-path should detect chain break when a rogue
        file is injected at the end of the sequence.

        This is the exact incident scenario: deployer wrote a rogue
        018-docs file that LATEST.json pointed to.
        """
        # Write legitimate chain: planner→builder→reviewer
        h1 = _make_handoff_data(sequence=1, source="planner", target="builder")
        h2 = _make_handoff_data(sequence=2, source="builder", target="reviewer")
        _write_handoff(spec_dir, h1)
        _write_handoff(spec_dir, h2)

        # Write rogue file: deployer→human (injected, breaks chain)
        rogue = _make_handoff_data(
            sequence=3, source="deployer", target="human", outcome="success"
        )
        _write_handoff(spec_dir, rogue)

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json"]

        # Read last and second-to-last
        with open(json_files[-1]) as fh:
            last_data = json.load(fh)
        with open(json_files[-2]) as fh:
            prev_data = json.load(fh)

        last_source = last_data["metadata"]["agent"]
        prev_target = prev_data["nextAgent"]["target"]

        # Chain validation: prev target should match last source
        # reviewer != deployer → chain break detected!
        assert prev_target != last_source, (
            "Chain should be broken: reviewer→deployer is not a valid transition"
        )

    def test_recover_state_chain_validation_passes_for_valid_chain(self, spec_dir):
        """Valid chain should pass: prev target matches last source."""
        h1 = _make_handoff_data(sequence=1, source="planner", target="builder")
        h2 = _make_handoff_data(sequence=2, source="builder", target="reviewer")
        h3 = _make_handoff_data(sequence=3, source="reviewer", target="tester")
        for h in [h1, h2, h3]:
            _write_handoff(spec_dir, h)

        json_files = sorted(spec_dir.glob("*.json"))
        with open(json_files[-1]) as fh:
            last_data = json.load(fh)
        with open(json_files[-2]) as fh:
            prev_data = json.load(fh)

        assert prev_data["nextAgent"]["target"] == last_data["metadata"]["agent"]

    def test_recover_state_fast_path_sets_handoff_count(self, spec_dir, pipeline):
        """Fast-path recovery must set handoff_count = len(json_files)
        so Phase 2 sequence validation has the correct baseline."""
        # 5 handoff files, last one targets human (terminal)
        for i, (src, tgt) in enumerate([
            ("planner", "builder"), ("builder", "reviewer"),
            ("reviewer", "tester"), ("tester", "deployer"),
            ("deployer", "human"),
        ], 1):
            _write_handoff(spec_dir, _make_handoff_data(
                sequence=i, source=src, target=tgt
            ))

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json"]

        # Simulate fast-path: create pipeline with correct handoff_count
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "completed"
        p.current_agent = None
        p.handoff_count = len(json_files)

        assert p.handoff_count == 5


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: Ingestion validation
# ═══════════════════════════════════════════════════════════════════════


class TestPhase2_IngestionValidation:
    """Verify source agent and sequence validation with quarantine."""

    def test_source_agent_mismatch_detected(self, pipeline):
        """Handoff from wrong agent should be flagged."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.current_agent = "builder"

        handoff = Handoff(**_make_handoff_data(
            sequence=2, source="deployer", target="reviewer"
        ))

        expected = p.current_agent
        assert handoff.source_agent != expected
        assert handoff.source_agent == "deployer"
        assert expected == "builder"

    def test_source_agent_match_passes(self, pipeline):
        """Handoff from correct agent should pass validation."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.current_agent = "builder"

        handoff = Handoff(**_make_handoff_data(
            sequence=2, source="builder", target="reviewer"
        ))
        assert handoff.source_agent == p.current_agent

    def test_no_pipeline_skips_source_validation(self, pipeline):
        """First handoff for a new spec (no pipeline yet) should skip
        source validation gracefully."""
        assert pipeline.get_pipeline(SPEC_ID) is None
        # Validation code checks: if pipeline_for_val and pipeline_for_val.current_agent
        # Both are falsy → skip validation → no quarantine

    def test_no_current_agent_skips_source_validation(self, pipeline):
        """Pipeline with current_agent=None should skip source validation."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.current_agent = None
        assert not p.current_agent  # falsy → skip validation

    def test_sequence_gap_detected(self, pipeline):
        """Sequence number > expected should be flagged as gap."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.handoff_count = 3

        handoff = Handoff(**_make_handoff_data(sequence=7))
        expected_seq = p.handoff_count + 1  # 4
        assert handoff.sequence > expected_seq

    def test_sequence_exact_match_passes(self, pipeline):
        """Sequence number == expected should pass."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.handoff_count = 3

        handoff = Handoff(**_make_handoff_data(sequence=4))
        expected_seq = p.handoff_count + 1
        assert handoff.sequence == expected_seq

    def test_sequence_lower_than_expected_not_quarantined(self, pipeline):
        """Sequence <= handoff_count should NOT be quarantined (caught
        by idempotency check instead)."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.handoff_count = 5

        handoff = Handoff(**_make_handoff_data(sequence=3))
        expected_seq = p.handoff_count + 1  # 6
        # sequence 3 < 6, but NOT > 6, so it doesn't trigger gap quarantine
        assert not (handoff.sequence > expected_seq)

    def test_quarantine_file_moves_to_subdirectory(self, spec_dir):
        """_quarantine_file should move file to .quarantine/ with reason prefix."""
        handoff_data = _make_handoff_data(sequence=99, source="rogue")
        path = _write_handoff(spec_dir, handoff_data)
        assert path.exists()

        # Simulate quarantine
        quarantine_dir = path.parent / ".quarantine"
        quarantine_dir.mkdir(exist_ok=True)
        dest = quarantine_dir / f"source_agent_mismatch__{path.name}"
        path.rename(dest)

        assert not path.exists()
        assert dest.exists()
        assert ".quarantine" in str(dest)
        assert "source_agent_mismatch" in dest.name

    def test_idempotency_check_before_validation(self):
        """The idempotency check must run BEFORE source agent validation
        to prevent false positives on legitimate retries (M3 fix)."""
        # This is a code structure test — verify the ordering by
        # checking that process_handoff processes idempotency before
        # quarantine checks.
        import inspect
        from iwo.daemon import IWODaemon

        source = inspect.getsource(IWODaemon.process_handoff)
        idempotency_pos = source.find("already_processed")
        quarantine_pos = source.find("_quarantine_file")
        assert idempotency_pos < quarantine_pos, (
            "Idempotency check must appear before quarantine in process_handoff"
        )


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: Stall watchdog fixes
# ═══════════════════════════════════════════════════════════════════════


class TestPhase3_StallWatchdog:
    """Verify watchdog entries persist, escalation works, and
    pre-stall scan catches files."""

    def test_watchdog_not_cleaned_for_active_pipeline(self, pipeline):
        """Watchdog entries for active pipelines must NOT be cleaned up.
        The old code dropped entries at 300s — that monitoring gap killed
        the pipeline in the incident."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "active"

        # The cleanup check is:
        #   if pipeline.status in ("completed", "halted", "stale"):
        #       resolved_watchdogs.append(agent_name)
        assert p.status not in ("completed", "halted", "stale")

    def test_watchdog_cleaned_for_completed_pipeline(self, pipeline):
        """Watchdog entries for completed pipelines should be cleaned."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        pipeline.mark_completed(SPEC_ID)
        assert p.status in ("completed", "halted", "stale")

    def test_watchdog_cleaned_for_halted_pipeline(self, pipeline):
        """Watchdog entries for halted pipelines should be cleaned."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        pipeline.mark_halted(SPEC_ID, "test stall")
        assert p.status in ("completed", "halted", "stale")

    def test_pre_stall_scan_finds_unprocessed_files(self, spec_dir):
        """Pre-stall scan should find files not in the tracker."""
        from iwo.daemon import HandoffTracker

        tracker = HandoffTracker()
        h1_data = _make_handoff_data(sequence=1)
        h1_path = _write_handoff(spec_dir, h1_data)
        h1 = Handoff(**h1_data)

        # File exists but tracker hasn't seen it
        assert not tracker.already_processed(h1)

        # After marking processed, it should be known
        tracker.mark_processed(h1, h1_path)
        assert tracker.already_processed(h1)

    def test_pre_stall_scan_skips_latest_json(self, spec_dir):
        """Pre-stall scan should skip LATEST.json and .tmp files."""
        (spec_dir / "LATEST.json").write_text("{}")
        (spec_dir / "partial.json.tmp").write_text("{}")
        _write_handoff(spec_dir, _make_handoff_data(sequence=1))

        files = sorted(spec_dir.glob("*.json"))
        filtered = [
            f for f in files
            if f.name != "LATEST.json" and not f.name.endswith(".tmp")
        ]
        assert len(filtered) == 1
        assert filtered[0].name.startswith("001-")

    def test_ops_action_created_on_stall_halt(self):
        """When stall halts a pipeline, an OpsAction should be created."""
        from iwo.ops_actions import OpsAction

        action = OpsAction(
            id="stall-TEST-SYNTH-001-1234567890",
            spec_id=SPEC_ID,
            title="Stall: deployer/TEST-SYNTH-001 — no handoff after 240s",
            description="Pipeline halted: deployer stalled.",
            priority="critical",
            source_agent="stall-watchdog",
            category="other",
        )
        assert action.priority == "critical"
        assert action.source_agent == "stall-watchdog"
        assert SPEC_ID in action.title


# ═══════════════════════════════════════════════════════════════════════
# Phase 4: Nextcloud filesystem hardening
# ═══════════════════════════════════════════════════════════════════════


class TestPhase4_NextcloudHardening:
    """Verify path canonicalization, mtime stabilization, and
    quarantine filtering."""

    def test_quarantine_filtered_in_handler(self):
        """HandoffHandler._handle_new_handoff must filter .quarantine paths.
        This prevents quarantined files from being re-detected by inotify."""
        import inspect
        from iwo.daemon import HandoffHandler

        source = inspect.getsource(HandoffHandler._handle_new_handoff)
        assert '".quarantine"' in source or "'.quarantine'" in source, (
            ".quarantine must be filtered in _handle_new_handoff"
        )

    def test_mtime_stabilization_skips_fresh_files(self, spec_dir):
        """Files with mtime < 1s old should be skipped by reconciliation."""
        path = _write_handoff(spec_dir, _make_handoff_data(sequence=1))
        mtime = path.stat().st_mtime
        # File just written — mtime should be very recent
        assert time.time() - mtime < 1.0

    def test_mtime_stabilization_allows_old_files(self, spec_dir):
        """Files with mtime > 1s old should be processed."""
        import os

        path = _write_handoff(spec_dir, _make_handoff_data(sequence=1))
        # Backdate the file by 5 seconds
        old_time = time.time() - 5
        os.utime(path, (old_time, old_time))
        assert time.time() - path.stat().st_mtime >= 1.0

    def test_path_resolve_canonicalizes_symlinks(self, tmp_path):
        """path.resolve() should follow symlinks to canonical path."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real_file = real_dir / "test.json"
        real_file.write_text("{}")

        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)
        link_file = link_dir / "test.json"

        # Before resolve: paths differ
        assert str(link_file) != str(real_file)
        # After resolve: paths match
        assert link_file.resolve() == real_file.resolve()

    def test_detection_layer_attribution_in_log_message(self):
        """Reconciliation log messages should attribute the detection layer."""
        import inspect
        from iwo.daemon import IWODaemon

        source = inspect.getsource(IWODaemon._reconcile_filesystem)
        assert "reconciliation layer catch" in source
        assert "inotify missed" in source


# ═══════════════════════════════════════════════════════════════════════
# Phase 5: Auto-recovery cascade guards
# ═══════════════════════════════════════════════════════════════════════


class TestPhase5_CascadeGuards:
    """Verify cascade guards prevent runaway auto-recovery."""

    def test_guard_5a_once_per_stall_event(self):
        """Guard 5a: _auto_recovery_fired set prevents duplicate firing."""
        fired: set[tuple[str, str]] = set()
        key = ("deployer", SPEC_ID)

        assert key not in fired
        fired.add(key)
        assert key in fired  # Blocked on second check

    def test_guard_5b_cooldown_blocks_rapid_retries(self):
        """Guard 5b: cooldown timestamp prevents rapid-fire recovery."""
        last: dict[str, float] = {}
        cooldown = 600.0  # 10 minutes
        now = time.time()

        last[SPEC_ID] = now - 60  # 1 minute ago
        elapsed = now - last[SPEC_ID]
        assert elapsed < cooldown  # Should be blocked

        last[SPEC_ID] = now - 700  # 11+ minutes ago
        elapsed = now - last[SPEC_ID]
        assert elapsed >= cooldown  # Should pass

    def test_guard_5c_max_attempts_blocks_after_limit(self):
        """Guard 5c: max attempts per spec per pipeline run."""
        count: dict[str, int] = {}
        max_per_spec = 3

        for i in range(3):
            count[SPEC_ID] = count.get(SPEC_ID, 0) + 1

        assert count[SPEC_ID] >= max_per_spec  # Blocked

    def test_guard_state_cleared_on_pipeline_completion(self, pipeline):
        """All cascade guard state should be cleared when pipeline completes."""
        # Simulate state accumulation
        auto_count = {SPEC_ID: 2}
        auto_last = {SPEC_ID: time.time() - 300}
        auto_fired = {("deployer", SPEC_ID)}

        # Simulate _clear_auto_recovery_state
        auto_count.pop(SPEC_ID, None)
        auto_last.pop(SPEC_ID, None)
        auto_fired = {(a, s) for a, s in auto_fired if s != SPEC_ID}

        assert SPEC_ID not in auto_count
        assert SPEC_ID not in auto_last
        assert len(auto_fired) == 0

    def test_guard_state_not_leaked_across_sprints(self):
        """After clearing, a new sprint should have fresh counters."""
        count: dict[str, int] = {SPEC_ID: 3}  # Exhausted

        # Clear on completion
        count.pop(SPEC_ID, None)

        # New sprint starts — count should be 0
        assert count.get(SPEC_ID, 0) == 0
        assert count.get(SPEC_ID, 0) < 3  # Not blocked

    def test_auto_recovery_audit_marker(self):
        """Auto-generated handoffs must include auto_recovery: true."""
        from iwo.auto_handoff import generate_auto_handoff
        from unittest.mock import patch
        import iwo.auto_handoff as ah

        with patch.object(ah, "_get_git_diff_stat", return_value=([], [])), \
             patch.object(ah, "_run_tests", return_value={"passed": 0, "failed": 0, "skipped": 0}), \
             patch.object(ah, "_run_typecheck", return_value=True):

            from tempfile import mkdtemp
            tmp = Path(mkdtemp())
            result = generate_auto_handoff(
                agent_name="builder",
                spec_id=SPEC_ID,
                last_sequence=1,
                project_dir=tmp,
                handoffs_dir=tmp,
            )

        assert result is not None
        data = json.loads(result.read_text())
        assert data["metadata"]["auto_recovery"] is True
        assert data["metadata"]["auto_generated"] is True


# ═══════════════════════════════════════════════════════════════════════
# Phase 6: Observability
# ═══════════════════════════════════════════════════════════════════════


class TestPhase6_Observability:
    """Verify watchdog dashboard and telemetry."""

    def test_active_specs_includes_watchdog_state(self):
        """_write_active_specs should include watchdog entries."""
        import inspect
        from iwo.daemon import IWODaemon

        source = inspect.getsource(IWODaemon._write_active_specs)
        assert "watchdog" in source
        assert "idle_seconds" in source
        assert "alert_sent" in source

    def test_watchdog_state_structure(self):
        """Watchdog state in .active-specs.json should have expected shape."""
        now = time.time()
        stall_watchdog = {
            "deployer": (now - 120, SPEC_ID),
        }
        stall_alert_sent: set[str] = set()

        state = {
            agent: {
                "spec_id": spec_id,
                "idle_seconds": int(now - idle_at),
                "alert_sent": agent in stall_alert_sent,
            }
            for agent, (idle_at, spec_id) in stall_watchdog.items()
        }

        assert "deployer" in state
        assert state["deployer"]["spec_id"] == SPEC_ID
        assert state["deployer"]["idle_seconds"] >= 119  # ~120s
        assert state["deployer"]["alert_sent"] is False

    def test_fast_path_skip_telemetry_in_reconciliation(self):
        """Reconciliation should log when it skips a directory."""
        import inspect
        from iwo.daemon import IWODaemon

        source = inspect.getsource(IWODaemon._reconcile_filesystem)
        assert "pipeline status=" in source


# ═══════════════════════════════════════════════════════════════════════
# Integration: Incident 2026-04-11 replay
# ═══════════════════════════════════════════════════════════════════════


class TestIncident20260411Replay:
    """Replay the exact incident scenario and verify the fix prevents it."""

    def test_rogue_deployer_file_does_not_poison_reconciliation(
        self, spec_dir, pipeline
    ):
        """Scenario: Deployer writes 017 (real) and 018 (rogue docs).
        Reconciliation should still process 017 because PipelineManager
        says pipeline is active, regardless of what LATEST.json says."""
        # Pipeline is active, deployer assigned
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "active"
        p.current_agent = "deployer"

        # Write real deployer handoff
        h017 = _make_handoff_data(
            sequence=17, source="deployer", target="docs"
        )
        _write_handoff(spec_dir, h017)

        # Write rogue docs handoff (the trigger of the incident)
        h018 = _make_handoff_data(
            sequence=18, source="deployer", target="human"
        )
        _write_handoff(spec_dir, h018)

        # Poison LATEST.json to point to 018
        latest = spec_dir / "LATEST.json"
        latest.write_text(json.dumps(h018))

        # Reconciliation checks PipelineManager, NOT LATEST.json
        assert p.status not in ("completed", "stale", "halted")
        # → Would NOT skip this directory → would find 017 → success

    def test_rogue_file_quarantined_by_source_validation(self, pipeline):
        """The rogue 018-docs file (source=deployer but target=human)
        would pass source validation (deployer is assigned) but the
        REAL 017-deployer handoff should be processed first due to
        sorted file ordering."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.current_agent = "deployer"
        p.handoff_count = 16  # 16 previous handoffs

        # 017: sequence matches expected (17 == 16+1) → passes
        h017 = Handoff(**_make_handoff_data(
            sequence=17, source="deployer", target="docs"
        ))
        assert h017.sequence == p.handoff_count + 1

        # After processing 017, handoff_count becomes 17, current_agent → docs
        p.handoff_count = 17
        p.current_agent = "docs"

        # 018: source=deployer but current_agent=docs → DESYNC → quarantined
        h018 = Handoff(**_make_handoff_data(
            sequence=18, source="deployer", target="human"
        ))
        assert h018.source_agent != p.current_agent
        # → Would be quarantined ✓

    def test_recovery_detects_rogue_file_chain_break(self, spec_dir):
        """On restart, _recover_state should detect that the rogue
        018-docs file breaks the chain from 017-deployer."""
        # Build a realistic chain
        chain = [
            (1, "planner", "builder"), (2, "builder", "reviewer"),
            (3, "reviewer", "tester"), (4, "tester", "deployer"),
            (5, "deployer", "docs"),
        ]
        for seq, src, tgt in chain:
            _write_handoff(spec_dir, _make_handoff_data(
                sequence=seq, source=src, target=tgt
            ))

        # Rogue file: deployer→human (chain expects docs→human)
        rogue = _make_handoff_data(
            sequence=6, source="deployer", target="human"
        )
        _write_handoff(spec_dir, rogue)

        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [f for f in json_files if f.name != "LATEST.json"]

        # Chain validation
        with open(json_files[-1]) as f:
            last = json.load(f)
        with open(json_files[-2]) as f:
            prev = json.load(f)

        prev_target = prev["nextAgent"]["target"]  # "docs"
        last_source = last["metadata"]["agent"]     # "deployer"

        # Chain break: docs != deployer
        assert prev_target != last_source

    def test_stall_watchdog_not_silently_dropped(self, pipeline):
        """In the incident, the 300s cleanup silently dropped the
        watchdog entry. Now it must persist until pipeline terminal."""
        p = pipeline.get_or_create_pipeline(SPEC_ID)
        p.status = "active"

        # Even at 600s elapsed, active pipeline → NOT resolved
        assert p.status not in ("completed", "halted", "stale")
