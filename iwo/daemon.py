"""IWO Daemon — Headless dispatch orchestrator.

Watches for handoff files, validates them, and dispatches to agents
via HeadlessCommander (deterministic ``claude -p`` subprocess invocation).

Key capabilities:
- HeadlessCommander dispatch (Phase 3): deterministic idle detection via
  pane_current_command, no canary probes or regex prompt matching
- Multi-spec pipeline tracking (PipelineManager) with rejection-first priority
- Automatic crash recovery (respawn-pane + re-launch Claude Code)
- Deploy gate with TUI manual approval flow
- Post-deploy health checks
- 30-second filesystem reconciliation
- Pipe-pane archival logging

Design: Three-model consensus (Claude Opus 4.6 + GPT-5.2 + Gemini 3 Pro).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

from .config import IWOConfig
from .parser import Handoff
from .headless_commander import HeadlessCommander
from .state import AgentState
from .memory import IWOMemory
from .pipeline import PipelineManager
from .metrics import MetricsCollector
from .auditor import Auditor, AuditorConfig
from .directives import DirectiveProcessor
from .ops_actions import (
    OpsAction,
    OpsActionsRegister,
    classify_category,
    classify_priority,
    compute_fingerprint,
    _next_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iwo.daemon")


class HandoffTracker:
    """Tracks processed handoffs to prevent duplicates (GPT-5.2's idempotency key).

    Phase 2.7: Supports supersede — if a newer file arrives with the same
    idempotency key, it replaces the original (e.g., Reviewer redoes work).
    """

    def __init__(self):
        self._processed: set[str] = set()
        self._processed_paths: dict[str, Path] = {}  # key → path for supersede check
        self._spec_handoff_counts: dict[str, int] = {}
        self._rejection_counts: dict[str, int] = {}  # key: "spec:agent_pair"

    def already_processed(self, handoff: Handoff, path: Optional[Path] = None) -> bool:
        """Check if handoff was already processed.

        If path is provided and a previous file with the same key exists,
        allow supersede if the new file is different (newer version from
        the same agent at the same sequence).
        """
        key = handoff.idempotency_key
        if key not in self._processed:
            return False

        # Same key exists — check for supersede
        if path and key in self._processed_paths:
            prev_path = self._processed_paths[key]
            if path != prev_path:
                log.info(
                    f"Supersede: {key} has newer file {path.name} "
                    f"(replacing {prev_path.name})"
                )
                # Allow re-processing — caller will process the newer version
                self._processed.discard(key)
                return False

        return True

    def mark_processed(self, handoff: Handoff, path: Optional[Path] = None):
        self._processed.add(handoff.idempotency_key)
        if path:
            self._processed_paths[handoff.idempotency_key] = path
        spec = handoff.spec_id
        self._spec_handoff_counts[spec] = self._spec_handoff_counts.get(spec, 0) + 1

    def check_rejection_loop(self, handoff: Handoff, max_loops: int) -> bool:
        """Returns True if rejection loop threshold exceeded."""
        if not handoff.is_rejection:
            return False
        key = f"{handoff.spec_id}:{handoff.source_agent}->{handoff.target_agent}"
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        count = self._rejection_counts[key]
        if count >= max_loops:
            log.warning(f"Rejection loop threshold ({max_loops}) hit: {key} ({count} times)")
            return True
        return False

    def check_handoff_limit(self, handoff: Handoff, max_handoffs: int) -> bool:
        """Returns True if handoff count limit exceeded for this spec."""
        count = self._spec_handoff_counts.get(handoff.spec_id, 0)
        return count >= max_handoffs


class HandoffHandler(FileSystemEventHandler):
    """Watchdog handler for new handoff JSON files.

    Handles both ``on_created`` and ``on_moved`` events because Nextcloud
    sync clients (and many editors) write to a temp file then rename/move
    into the final path.  The rename triggers ``on_moved`` (not
    ``on_created``), so we must handle both to reliably detect handoffs.
    """

    def __init__(self, daemon: "IWODaemon"):
        self.daemon = daemon

    # -- public watchdog callbacks -----------------------------------------

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        self._handle_new_handoff(Path(event.src_path))

    def on_moved(self, event: FileMovedEvent):
        """Catch Nextcloud's atomic .tmp → .json rename pattern."""
        if event.is_directory:
            return
        self._handle_new_handoff(Path(event.dest_path))

    # -- shared logic ------------------------------------------------------

    def _handle_new_handoff(self, path: Path) -> None:
        """Validate *path* and forward to the daemon for processing."""
        # ── Phase 4 fix 4a: Resolve symlinks to canonical path ────────
        # Nextcloud sometimes creates symlinks or intermediate paths that
        # differ from the canonical path, causing duplicate processing or
        # path comparison mismatches.  See incident 2026-04-11.
        try:
            path = path.resolve()
        except OSError:
            pass  # Fall through with original path if resolve fails
        if path.suffix != ".json":
            return
        if path.name == "LATEST.json":
            return
        if path.name.endswith(".tmp"):
            return
        # Ignore audit trail files (written by auditor, not handoffs)
        if ".audit" in path.parts:
            log.debug(f"Skipping audit file: {path.name}")
            return
        # Ignore directive files (processed by DirectiveProcessor, not handoff pipeline)
        if ".directives" in path.parts:
            log.debug(f"Skipping directive file: {path.name}")
            return
        # Ignore quarantined files (moved by _quarantine_file, not real handoffs)
        if ".quarantine" in path.parts:
            log.debug(f"Skipping quarantined file: {path.name}")
            return
        # NOTE: LATEST.json is updated ONLY in process_handoff() after
        # validation (via _update_latest). We must NOT update it here
        # pre-validation — a rogue file would corrupt the symlink and
        # poison the reconciliation fast-path. See incident 2026-04-11.
        log.info(f"New handoff detected (inotify): {path.name}")
        time.sleep(self.daemon.config.file_debounce_seconds)
        self.daemon.process_handoff(path)


class IWODaemon:
    """Main orchestrator daemon — Phase 1.0."""

    def __init__(self, config: Optional[IWOConfig] = None):
        self.config = config or IWOConfig()
        self.commander = HeadlessCommander(self.config)
        self.tracker = HandoffTracker()
        self.observer: Optional[Observer] = None

        # Agent state tracking (Phase 2 headless — replaces AgentStateMachine)
        self.agent_states: dict[str, AgentState] = {}
        self._state_changed_at: dict[str, float] = {}

        # Pending activations: handoffs waiting for target agent to become IDLE
        self._pending_activations: list[tuple[Handoff, Path]] = []

        # Phase 2: handoff history for dashboard display (most recent first)
        self.handoff_history: list[Handoff] = []
        self._max_history: int = 50

        # Startup timestamp — used for session-based staleness (Option B)
        # Any handoff file older than this timestamp is from a previous session
        # and should NOT cause agent assignments during recovery.
        self._started_at: float = time.time()
        self._session_id: str = time.strftime("%Y%m%d-%H%M%S")

        # Phase 2.1: Memory integration
        self.memory: Optional[IWOMemory] = None

        # Phase 2.3: Multi-spec pipeline manager
        self.pipeline = PipelineManager(max_concurrent=self.config.max_concurrent_specs)

        # Phase 2.4.1: Crash recovery tracking
        self._respawn_attempts: dict[str, int] = {}  # agent_name → attempt count
        self._respawn_cooldown: dict[str, float] = {}  # agent_name → last attempt time

        # Phase 3: Deploy gate — FIFO queue of gated handoffs for TUI approval
        self._deploy_gate_pending: list[tuple[Handoff, Path]] = []

        # Phase 2.9.1: Stall detection — alert when agent completes without handoff
        # Maps agent_name → (idle_timestamp, spec_id) — set on PROCESSING→IDLE,
        # cleared when handoff is processed for that spec. If entry survives 60s,
        # fire ntfy alert.
        self._stall_watchdog: dict[str, tuple[float, str]] = {}
        self._stall_alert_sent: set[str] = set()  # avoid duplicate alerts

        # Phase 5: Auto-recovery cascade guards (incident 2026-04-11)
        # Tracks recovery attempts to prevent phantom-handoff cascades.
        # spec_id → count of auto-recovery attempts this pipeline run
        self._auto_recovery_count: dict[str, int] = {}
        # spec_id → timestamp of last auto-recovery attempt
        self._auto_recovery_last: dict[str, float] = {}
        # Set of (agent_name, spec_id) tuples that already fired once
        # for the current stall event (cleared when watchdog is cleared)
        self._auto_recovery_fired: set[tuple[str, str]] = set()

        # Phase 2.5.1: Metrics collector (initialized after memory)
        self.metrics: Optional[MetricsCollector] = None

        # Phase 3.0: Auditor module (Agent 007 Phase 1)
        self.auditor: Optional[Auditor] = None

        # Directive processor — operator commands via filesystem
        self.directive_processor = DirectiveProcessor(self.config, self)

        # Ops Actions Register — tracks human-required operational tasks
        ops_path = self.config.handoffs_dir / ".ops-actions.json"
        self.ops_register = OpsActionsRegister(ops_path)
        self.ops_register.load()

        # Pause flag — set by pause directive, prevents new dispatches
        self._paused: bool = False

        # Ops agent: gate pending data + proactive check timer
        self._ops_gate_pending: Optional[tuple] = None
        self._last_ops_proactive_check: float = 0.0

        # State-change notification debounce: agent_name → last notify timestamp
        # Prevents notification spam when agents flicker between states rapidly
        self._state_notify_debounce: dict[str, float] = {}
        self._state_notify_cooldown: float = 30.0  # seconds between state notifications per agent

    def _init_agent_states(self):
        """Initialize agent state tracking for all discovered agents.

        Headless Phase 2: states derived from HeadlessCommander methods,
        no AgentStateMachine needed.
        """
        self.agent_states.clear()
        self._state_changed_at.clear()
        now = time.time()
        for name in self.commander.discovered_agents:
            self.agent_states[name] = AgentState.UNKNOWN
            self._state_changed_at[name] = now
            log.info(f"Agent state initialized: {name} → UNKNOWN")

    def _poll_agent_states(self):
        """Poll all agents for state changes. Called every ~2s from main loop.

        Headless Phase 2: derives state from HeadlessCommander deterministic
        checks — no AgentStateMachine, no canary probes.
        - name in commander.active_agents → PROCESSING
        - commander.is_agent_idle(name) → IDLE
        - otherwise → UNKNOWN
        """
        now = time.time()
        active = self.commander.active_agents

        # Check for completed agents (pane returned to idle shell)
        completed = self.commander.check_completions()
        for name in completed:
            prev = self.agent_states.get(name, AgentState.UNKNOWN)
            self.agent_states[name] = AgentState.IDLE
            self._state_changed_at[name] = now
            if prev != AgentState.IDLE:
                log.info(f"[{name}] {prev.value} → idle (completed)")
                if prev == AgentState.PROCESSING:
                    self._notify_state_change(name, prev, AgentState.IDLE, now)
                    # Stall watchdog: record that this agent finished work
                    # Only arm if agent has a real pipeline assignment —
                    # otherwise we create phantom "unknown" pipelines (Bug 1)
                    spec_id = self.pipeline.agent_current_spec(name)
                    if spec_id:
                        self._stall_watchdog[name] = (now, spec_id)
                        self._stall_alert_sent.discard(name)

        # Update all agent states
        for name in self.agent_states:
            if name in completed:
                continue  # already handled above
            prev = self.agent_states[name]

            if name in active:
                new_state = AgentState.PROCESSING
            elif self.commander.is_agent_idle(name):
                new_state = AgentState.IDLE
            else:
                new_state = AgentState.UNKNOWN

            if new_state != prev:
                self.agent_states[name] = new_state
                self._state_changed_at[name] = now
                log.info(f"[{name}] {prev.value} → {new_state.value}")
                self._notify_state_change(name, prev, new_state, now)

        # Check if any pending activations can proceed
        self._process_pending_activations()

        # Phase 2.9.2: Stall detection + auto-handoff recovery
        # Refactored in Phase 3 fix (incident 2026-04-11):
        #  - Pre-stall scan: before declaring stall, check for unprocessed handoffs
        #  - Never silently drop watchdog entries for active pipelines
        #  - Escalate to Ops Register + mark pipeline halted instead of silent cleanup
        from iwo.auto_handoff import generate_auto_handoff

        stall_timeout = self.config.stall_alert_timeout
        auto_handoff_timeout = self.config.stall_auto_handoff_timeout
        resolved_watchdogs = []
        for agent_name, (idle_at, spec_id) in list(self._stall_watchdog.items()):
            elapsed = now - idle_at

            # ── Phase 3 fix 3c: Pre-stall directory scan ──────────────
            # Before declaring a stall at the warning threshold, do an
            # explicit scan of the spec's agent-comms directory.  If
            # unprocessed handoff files exist, process them — this is the
            # last-resort catch for inotify + reconciliation both missing
            # a file.  See incident 2026-04-11.
            if elapsed >= stall_timeout and agent_name not in self._stall_alert_sent:
                spec_dir = self.config.handoffs_dir / spec_id
                if spec_dir.exists():
                    for f in sorted(spec_dir.glob("*.json")):
                        if f.name == "LATEST.json" or f.name.endswith(".tmp"):
                            continue
                        # Mtime stabilization (same as reconciliation Phase 4b)
                        try:
                            if time.time() - f.stat().st_mtime < 1.0:
                                log.debug(f"Pre-stall scan: skipping {f.name} (mtime unstable)")
                                continue
                        except OSError:
                            continue
                        try:
                            with open(f) as fh:
                                data = json.load(fh)
                            handoff = Handoff(**data)
                            if not self.tracker.already_processed(handoff):
                                log.warning(
                                    f"PRE-STALL SCAN: found unprocessed {f.name} "
                                    f"for {agent_name}/{spec_id} — processing now"
                                )
                                self.process_handoff(f)
                        except Exception as e:
                            log.debug(f"Pre-stall scan: skip {f.name}: {e}")
                    # Re-check: if watchdog was cleared by process_handoff
                    # (which discards stall_alert_sent), we're done
                    if agent_name not in self._stall_watchdog:
                        continue

            if (
                elapsed >= auto_handoff_timeout
                and agent_name not in self._stall_alert_sent
            ):
                if self.config.stall_auto_handoff_enabled:
                    # ── Phase 5: Cascade guards ───────────────────────
                    # All four guards must pass before auto-recovery fires.
                    # If any guard blocks, fall through to the escalation
                    # path (Phase 3 fix 3b) which halts the pipeline.

                    # Guard 5a: Once per spec per stall event
                    guard_key = (agent_name, spec_id)
                    if guard_key in self._auto_recovery_fired:
                        log.warning(
                            f"AUTO-RECOVERY BLOCKED (5a): already fired for "
                            f"{agent_name}/{spec_id} this stall event"
                        )
                    # Guard 5b: Cooldown between attempts for same spec
                    # NOTE: Don't add to _stall_alert_sent here — we want
                    # to re-evaluate after cooldown expires on next poll.
                    elif (
                        spec_id in self._auto_recovery_last
                        and now - self._auto_recovery_last[spec_id]
                        < self.config.auto_recovery_cooldown_seconds
                    ):
                        remaining = int(
                            self.config.auto_recovery_cooldown_seconds
                            - (now - self._auto_recovery_last[spec_id])
                        )
                        log.debug(
                            f"AUTO-RECOVERY BLOCKED (5b): cooldown for "
                            f"{spec_id}, {remaining}s remaining"
                        )
                        continue  # Skip _stall_alert_sent — retry later
                    # Guard 5c: Max attempts per spec per pipeline run
                    elif (
                        self._auto_recovery_count.get(spec_id, 0)
                        >= self.config.auto_recovery_max_per_spec
                    ):
                        log.warning(
                            f"AUTO-RECOVERY BLOCKED (5c): {spec_id} hit max "
                            f"{self.config.auto_recovery_max_per_spec} attempts"
                        )
                        # Exceeded retries — escalate to halt
                        self.pipeline.mark_halted(
                            spec_id,
                            f"auto-recovery exhausted: {self._auto_recovery_count[spec_id]} "
                            f"attempts for {agent_name}"
                        )
                        self._clear_auto_recovery_state(spec_id)
                        self._notify(
                            f"🛑 PIPELINE HALTED: {agent_name}/{spec_id} — "
                            f"auto-recovery exhausted after "
                            f"{self._auto_recovery_count[spec_id]} attempts.",
                            critical=True,
                        )
                    else:
                        # All guards passed — proceed with auto-recovery
                        log.warning(
                            f"STALL AUTO-RECOVERY: generating handoff for "
                            f"{agent_name}/{spec_id} (idle {int(elapsed)}s)"
                        )
                        spec_dir = self.config.handoffs_dir / spec_id
                        existing = (
                            sorted(spec_dir.glob("*.json"))
                            if spec_dir.exists()
                            else []
                        )
                        last_seq = len(existing)

                        result = generate_auto_handoff(
                            agent_name=agent_name,
                            spec_id=spec_id,
                            last_sequence=last_seq,
                            project_dir=self.config.project_root,
                            handoffs_dir=self.config.handoffs_dir,
                        )

                        if result:
                            # Track for cascade guards
                            self._auto_recovery_fired.add(guard_key)
                            self._auto_recovery_last[spec_id] = now
                            self._auto_recovery_count[spec_id] = (
                                self._auto_recovery_count.get(spec_id, 0) + 1
                            )
                            log.info(
                                f"STALL AUTO-RECOVERY: wrote {result.name} "
                                f"(attempt {self._auto_recovery_count[spec_id]}/"
                                f"{self.config.auto_recovery_max_per_spec})"
                            )
                            self._notify(
                                f"🔧 Auto-recovered stall: {agent_name}/{spec_id} "
                                f"— generated {result.name} "
                                f"(attempt {self._auto_recovery_count[spec_id]}/"
                                f"{self.config.auto_recovery_max_per_spec})",
                                critical=True,
                            )
                        else:
                            log.error(
                                f"STALL AUTO-RECOVERY: failed for {agent_name}/{spec_id}"
                            )
                            self._notify(
                                f"⚠️ STALL: {agent_name}/{spec_id} — auto-recovery "
                                f"FAILED, manual intervention needed",
                                critical=True,
                            )
                else:
                    # ── Phase 3 fix 3b: Escalate instead of silent alert ──
                    # Mark pipeline as halted and escalate to Ops Register.
                    # This replaces the old silent stall alert that did nothing
                    # actionable.  See incident 2026-04-11.
                    log.warning(
                        f"STALL DETECTED → HALTING: {agent_name}/{spec_id} idle "
                        f"{int(elapsed)}s (auto-recovery disabled) — escalating"
                    )
                    self.pipeline.mark_halted(
                        spec_id,
                        f"stall: {agent_name} idle {int(elapsed)}s, no handoff detected"
                    )
                    self._clear_auto_recovery_state(spec_id)
                    self._notify(
                        f"🛑 PIPELINE HALTED: {agent_name}/{spec_id} — stall "
                        f"after {int(elapsed)}s, no handoff. Escalated to Ops Register.",
                        critical=True,
                    )
                    # Add ops action for human intervention
                    try:
                        from iwo.ops_actions import OpsAction
                        action = OpsAction(
                            id=f"stall-{spec_id}-{int(now)}",
                            spec_id=spec_id,
                            title=f"Stall: {agent_name}/{spec_id} — no handoff after {int(elapsed)}s",
                            description=f"Pipeline halted: {agent_name} stalled after "
                                        f"{int(elapsed)}s with no handoff. Check agent-comms "
                                        f"directory and agent logs.",
                            priority="critical",
                            source_agent="stall-watchdog",
                            category="other",
                        )
                        if self.ops_register.add(action):
                            self.ops_register.save()
                            log.info(f"Ops Register: added stall action {action.id}")
                    except Exception as e:
                        log.warning(f"Failed to add stall to Ops Register: {e}")

                self._stall_alert_sent.add(agent_name)

            elif (
                elapsed >= stall_timeout
                and agent_name not in self._stall_alert_sent
            ):
                log.warning(
                    f"STALL WARNING: {agent_name} completed {spec_id} "
                    f"{int(elapsed)}s ago, no handoff. Auto-recovery in "
                    f"{int(auto_handoff_timeout - elapsed)}s"
                )

            # ── Phase 3 fix 3a: Only clean up watchdog entries for
            # pipelines that have reached a terminal state.  The old code
            # silently dropped entries at 300s, creating a monitoring gap
            # where no detection layer watched the spec.  Now entries
            # persist until the pipeline is completed/halted/stale.
            # See incident 2026-04-11.
            pipeline = self.pipeline.get_pipeline(spec_id)
            if pipeline and pipeline.status in ("completed", "halted", "stale"):
                resolved_watchdogs.append(agent_name)

        for agent_name in resolved_watchdogs:
            # Look up spec_id before popping so we can clean up Phase 5 state
            entry = self._stall_watchdog.get(agent_name)
            if entry:
                _, resolved_spec = entry
                self._auto_recovery_fired.discard((agent_name, resolved_spec))
            self._stall_watchdog.pop(agent_name, None)
            self._stall_alert_sent.discard(agent_name)

        # Periodic staleness cleanup (Bug 3 fix) — release agents from idle pipelines
        stale_threshold = self.config.stale_pipeline_hours * 3600
        released = self.pipeline.release_stale_pipelines(stale_threshold)
        if released:
            self._notify(f"🧹 Released {len(released)} stale pipeline(s): {', '.join(released)}")

    def _notify_state_change(
        self, agent: str, prev: AgentState, new: AgentState, now: float
    ):
        """Send a push notification for significant agent state transitions.

        Debounced per-agent to avoid notification spam when agents flicker
        between states rapidly.  Only PROCESSING→IDLE and *→PROCESSING
        transitions trigger notifications; other transitions are too noisy.
        """
        # Only notify on significant transitions
        if new == AgentState.PROCESSING:
            msg = f"🚀 {agent} started working"
        elif new == AgentState.IDLE and prev == AgentState.PROCESSING:
            msg = f"✅ {agent} finished work"
        else:
            return  # UNKNOWN transitions are not worth a push notification

        # Debounce: skip if last notification for this agent was < cooldown ago
        last = self._state_notify_debounce.get(agent, 0.0)
        if now - last < self._state_notify_cooldown:
            log.debug(f"State notification suppressed for {agent} (debounce)")
            return

        self._state_notify_debounce[agent] = now
        self._notify(msg)

    def _attempt_respawn(self, agent_name: str):
        """Attempt to respawn a crashed agent. Max 3 attempts with 30s cooldown.

        Phase 2.4.1: Auto-recovery for crashed panes.
        """
        max_attempts = self.config.max_respawn_attempts
        cooldown = self.config.respawn_cooldown_seconds
        now = time.time()

        # Check cooldown
        last_attempt = self._respawn_cooldown.get(agent_name, 0)
        if now - last_attempt < cooldown:
            remaining = int(cooldown - (now - last_attempt))
            log.info(f"Respawn: {agent_name} in cooldown ({remaining}s remaining)")
            return

        # Check attempt count
        attempts = self._respawn_attempts.get(agent_name, 0)
        if attempts >= max_attempts:
            msg = (
                f"💀 {agent_name} permanently crashed — "
                f"exhausted {max_attempts} respawn attempts. Manual intervention required."
            )
            log.error(msg)
            self._notify(msg, critical=True)
            return

        # Attempt respawn
        self._respawn_attempts[agent_name] = attempts + 1
        self._respawn_cooldown[agent_name] = now
        attempt_num = attempts + 1

        log.info(f"Respawn: attempting {agent_name} (attempt {attempt_num}/{max_attempts})")
        self._notify(f"🔄 Respawning {agent_name} (attempt {attempt_num}/{max_attempts})")

        success = self.commander.respawn_agent(agent_name)

        if success:
            # Reset agent state — will be detected as IDLE on next poll
            self.agent_states[agent_name] = AgentState.UNKNOWN
            self._state_changed_at[agent_name] = time.time()

            self._notify(f"✅ {agent_name} respawned successfully (attempt {attempt_num})")
            log.info(f"Respawn: {agent_name} recovered on attempt {attempt_num}")

            # Log crash event to memory
            if self.memory:
                try:
                    self._log_crash_event(agent_name, attempt_num, recovered=True)
                except Exception as e:
                    log.warning(f"Memory: crash event logging failed: {e}")
        else:
            self._notify(
                f"❌ {agent_name} respawn failed (attempt {attempt_num}/{max_attempts})",
                critical=True,
            )
            log.warning(f"Respawn: {agent_name} failed on attempt {attempt_num}")

            if self.memory:
                try:
                    self._log_crash_event(agent_name, attempt_num, recovered=False)
                except Exception as e:
                    log.warning(f"Memory: crash event logging failed: {e}")

    def _log_crash_event(self, agent_name: str, attempt: int, recovered: bool):
        """Store crash event to memory for pattern analysis."""
        if not self.memory or not self.memory._neo4j_driver:
            return

        try:
            with self.memory._neo4j_driver.session() as session:
                session.run(
                    """
                    CREATE (c:CrashEvent {
                        agent: $agent,
                        timestamp: datetime(),
                        attempt: $attempt,
                        recovered: $recovered,
                        active_spec: $spec
                    })
                    """,
                    agent=agent_name,
                    attempt=attempt,
                    recovered=recovered,
                    spec=self.pipeline.agent_current_spec(agent_name) or "none",
                )
            log.info(f"Memory: crash event logged for {agent_name}")
        except Exception as e:
            log.warning(f"Memory: crash event log failed: {e}")

    def _run_post_deploy_health_check(self, handoff: Handoff):
        """Hit production URLs after a successful deploy to verify health.

        Phase 2.4.2: Runs after deployer reports success. Checks each URL
        for expected HTTP status within timeout. Non-blocking — failures
        notify but don't halt the pipeline.
        """
        import urllib.request
        import urllib.error

        spec_id = handoff.spec_id
        delay = self.config.health_check_delay
        timeout = self.config.health_check_timeout
        expected = self.config.health_check_expected_status

        log.info(f"Post-deploy health check: {spec_id} — waiting {delay}s for propagation")
        self._notify(f"🏥 Running post-deploy health check for {spec_id}...")
        time.sleep(delay)

        results: list[tuple[str, bool, str]] = []  # (url, passed, detail)

        for url in self.config.health_check_urls:
            try:
                req = urllib.request.Request(url, method="GET")
                req.add_header("User-Agent", "IWO-HealthCheck/2.4")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    if status == expected:
                        results.append((url, True, f"HTTP {status}"))
                        log.info(f"Health check PASS: {url} → HTTP {status}")
                    else:
                        results.append((url, False, f"HTTP {status} (expected {expected})"))
                        log.warning(f"Health check FAIL: {url} → HTTP {status}")
            except urllib.error.HTTPError as e:
                results.append((url, False, f"HTTP {e.code}"))
                log.warning(f"Health check FAIL: {url} → HTTP {e.code}")
            except urllib.error.URLError as e:
                results.append((url, False, f"Connection error: {e.reason}"))
                log.warning(f"Health check FAIL: {url} → {e.reason}")
            except Exception as e:
                results.append((url, False, f"Error: {e}"))
                log.warning(f"Health check FAIL: {url} → {e}")

        # Summarize
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)

        if passed == total:
            msg = f"✅ Post-deploy health check PASSED for {spec_id} ({passed}/{total} URLs)"
            log.info(msg)
            self._notify(msg)
        else:
            failed_details = [
                f"  {url}: {detail}" for url, ok, detail in results if not ok
            ]
            msg = (
                f"⚠️ Post-deploy health check FAILED for {spec_id} "
                f"({passed}/{total} passed)\n" + "\n".join(failed_details)
            )
            log.error(msg)
            self._notify(
                f"⚠️ HEALTH CHECK FAILED: {spec_id} — {total - passed} URL(s) down. "
                f"Consider rollback.",
                critical=True,
            )

        # Log to memory
        if self.memory and self.memory._neo4j_driver:
            try:
                with self.memory._neo4j_driver.session() as session:
                    session.run(
                        """
                        CREATE (h:HealthCheck {
                            spec_id: $spec_id,
                            timestamp: datetime(),
                            passed: $passed,
                            total: $total,
                            all_passed: $all_passed,
                            details: $details
                        })
                        """,
                        spec_id=spec_id,
                        passed=passed,
                        total=total,
                        all_passed=passed == total,
                        details=json.dumps(
                            [{u: d} for u, _, d in results]
                        ),
                    )
            except Exception as e:
                log.warning(f"Memory: health check logging failed: {e}")

    def _process_pending_activations(self):
        """Drain queued handoffs to idle agents.

        Headless Phase 2: uses deterministic is_agent_idle() check.
        No canary probes — pane_current_command is the idle signal.
        """
        # Legacy pending list (Phase 1 — migrate items to pipeline queue)
        if self._pending_activations:
            for handoff, path in self._pending_activations:
                self.pipeline.enqueue(handoff, path)
            self._pending_activations.clear()
            log.info("Migrated legacy pending activations to pipeline queue")

        # Check each agent's queue
        for name in list(self.agent_states.keys()):
            # Skip agents with no queued work
            if self.pipeline.queue_depth(name) == 0:
                continue
            # Skip if pipeline thinks agent is busy on a CURRENT spec
            if self.pipeline.is_agent_busy(name):
                continue
            # Skip if agent is not idle (deterministic check)
            if not self.commander.is_agent_idle(name):
                continue

            queued = self.pipeline.dequeue(name)
            if queued:
                log.info(
                    f"Queue drain: {name} idle — dispatching "
                    f"{queued.spec_id} #{queued.handoff.sequence}"
                )
                self._activate_for_handoff(name, queued.handoff, queued.path)

    def _activate_for_handoff(self, agent: str, handoff: Handoff, path: Path):
        """Send activation command to an agent and update pipeline tracking."""
        log.info(f"Activating {agent} for {handoff.spec_id} #{handoff.sequence}")
        success = self.commander.activate_agent(agent, handoff=handoff, handoff_path=path)
        if success:
            # Mark agent as processing immediately
            self.agent_states[agent] = AgentState.PROCESSING
            self._state_changed_at[agent] = time.time()
            self.pipeline.assign_agent(agent, handoff.spec_id)
            self._notify(f"✅ Activated {agent} for {handoff.spec_id} (#{handoff.sequence})")
            # Emit INFO audit event for phone notification of successful handoffs
            if self.auditor:
                from iwo.auditor import AuditEvent, Severity
                self.auditor._emit(AuditEvent(
                    timestamp=self.auditor._now_iso(),
                    check="handoff_success",
                    severity=Severity.INFO,
                    spec_id=handoff.spec_id,
                    details={
                        "agent": agent,
                        "sequence": handoff.sequence,
                        "message": f"✅ {agent} activated (verified) for {handoff.spec_id} (#{handoff.sequence})",
                    },
                    action_taken=f"activated_{agent}_verified",
                    recommended_action=None,
                ))
        else:
            # Re-queue on failure — will retry next poll cycle
            self.pipeline.enqueue(handoff, path)
            self._notify(f"❌ Failed to activate {agent}, re-queued", critical=True)

    def _should_auto_approve_deploy(
        self, path: Path, handoff: Handoff
    ) -> tuple[bool, str]:
        """Check if a deploy handoff can bypass the human gate.

        Returns (approved, reason) where *reason* is a human-readable string
        explaining why auto-approval succeeded or failed.  The caller uses
        *reason* in both the log and the TUI notification so the operator
        knows exactly what to check before pressing 'd'.

        Auto-approves when the handoff explicitly declares no infrastructure
        changes (noNewMigrations, noNewSecrets, noNewWranglerVars all True).

        Reads the raw JSON because these fields live in deploymentInstructions
        which is not part of the Pydantic Handoff model.
        """
        if not self.config.auto_approve_safe_deploys:
            return False, "auto-approve disabled in config"

        try:
            with open(path) as f:
                raw = json.load(f)
        except Exception:
            log.warning("Auto-approve: cannot read raw handoff, requiring manual approval")
            return False, "could not read handoff JSON"

        # Check deploymentInstructions block (used by Planner/Tester handoffs)
        deploy_info = raw.get("deploymentInstructions")

        if deploy_info is None:
            return (
                False,
                "no deploymentInstructions block in handoff — "
                "safety flags not provided by source agent",
            )

        no_migrations = deploy_info.get("noNewMigrations", False)
        no_secrets = deploy_info.get("noNewSecrets", False)
        no_vars = deploy_info.get("noNewWranglerVars", False)

        # All three must be explicitly True for auto-approval
        if no_migrations and no_secrets and no_vars:
            log.info(
                f"Auto-approve check: migrations={no_migrations}, "
                f"secrets={no_secrets}, vars={no_vars} → SAFE"
            )
            return True, "all safety flags True (no infra changes)"

        # Build specific reason listing which flags are missing/false
        flags = {
            "noNewMigrations": no_migrations,
            "noNewSecrets": no_secrets,
            "noNewWranglerVars": no_vars,
        }
        missing = [k for k, v in flags.items() if not v]
        reason = f"infrastructure flags missing or false: {', '.join(missing)}"
        log.info(f"Auto-approve check: UNSAFE — {reason}")
        return False, reason

    # --- Ops Actions Auto-Extraction ---

    # Patterns that indicate a human operational task (not code review notes)
    _OPS_PATTERNS = [
        re.compile(p, re.IGNORECASE) for p in [
            r'migration.*not\s+(yet\s+)?(applied|run)',
            r'not\s+yet\s+(set|configured|created|applied|deployed|stored)',
            r'wrangler\s+(secret|d1)',
            r'must\s+(run|create|configure|set|apply|execute|deploy|store)',
            r'secrets?\s+not\s+(set|configured)',
            r'\bDNS\b.*\b(CNAME|A\s+record|MX|DKIM|SPF|DMARC)\b',
            r'HUMAN\s+ACTION\s+REQUIRED',
            r'human\s+(must|task)',
            r'seed\s+data\s+not\s+(yet\s+)?loaded',
            r'not\s+yet\s+deployed',
            r'webhook.*not\s+(set|configured)',
            r'SMTP.*not\s+configured',
            r'Stripe\s+(product|webhook).*not\s+(yet\s+)?(created|configured)',
            r'n8n\s+workflow',
            r'KV\s+namespace.*not\s+(yet\s+)?created',
            r'KV\s+namespace\s+ID\s+(is\s+)?empty',
            r'npx\s+wrangler',
        ]
    ]

    def _is_ops_action(self, text: str) -> bool:
        """Check if text matches an operational action pattern."""
        return any(p.search(text) for p in self._OPS_PATTERNS)

    def _extract_ops_actions(self, handoff: Handoff, path: Path):
        """Extract ops actions from a handoff and add to the register.

        Scans unresolvedIssues, deploymentInstructions, and nextAgent for
        human-required operational tasks. Deduplicates via fingerprinting.
        Also runs stale detection for existing actions on this spec.
        """
        spec_id = handoff.spec_id
        new_actions: list[OpsAction] = []

        try:
            with open(path) as f:
                raw = json.load(f)
        except Exception as e:
            log.warning(f"Ops extraction: cannot read {path.name}: {e}")
            return

        source_agent = handoff.source_agent
        sequence = handoff.sequence
        existing_ids = [a.id for a in self.ops_register.actions]

        # Collect candidate texts for stale detection
        current_texts: set[str] = set()

        # 1. unresolvedIssues
        for issue in raw.get("status", {}).get("unresolvedIssues", []):
            if isinstance(issue, str) and len(issue.strip()) > 10:
                current_texts.add(issue.strip().lower())
                if self._is_ops_action(issue):
                    fp = compute_fingerprint(spec_id, issue)
                    new_actions.append(OpsAction(
                        id=_next_id(existing_ids + [a.id for a in new_actions]),
                        spec_id=spec_id,
                        title=issue.strip()[:80],
                        description=issue.strip(),
                        category=classify_category(issue),
                        priority=classify_priority(issue),
                        source_agent=source_agent,
                        source_sequence=sequence,
                        fingerprint=fp,
                        auto_extracted=True,
                    ))

        # 2. deploymentInstructions manual steps
        deploy_info = raw.get("deploymentInstructions", {})
        if isinstance(deploy_info, dict):
            for field in ("preDeploySteps", "postDeploySteps", "manualSteps"):
                steps = deploy_info.get(field, [])
                if isinstance(steps, list):
                    for step in steps:
                        if isinstance(step, str) and len(step.strip()) > 10:
                            current_texts.add(step.strip().lower())
                            if self._is_ops_action(step):
                                fp = compute_fingerprint(spec_id, step)
                                new_actions.append(OpsAction(
                                    id=_next_id(existing_ids + [a.id for a in new_actions]),
                                    spec_id=spec_id,
                                    title=step.strip()[:80],
                                    description=step.strip(),
                                    category=classify_category(step),
                                    priority=classify_priority(step),
                                    source_agent=source_agent,
                                    source_sequence=sequence,
                                    fingerprint=fp,
                                    auto_extracted=True,
                                ))

            # Infrastructure flags
            if deploy_info.get("noNewMigrations") is False:
                text = f"D1 migration required for {spec_id} — noNewMigrations=false"
                fp = compute_fingerprint(spec_id, text)
                new_actions.append(OpsAction(
                    id=_next_id(existing_ids + [a.id for a in new_actions]),
                    spec_id=spec_id,
                    title=f"D1 migration required for {spec_id}",
                    description=text,
                    category="migration",
                    priority="critical",
                    source_agent=source_agent,
                    source_sequence=sequence,
                    fingerprint=fp,
                    auto_extracted=True,
                ))

            if deploy_info.get("noNewSecrets") is False:
                text = f"Wrangler secrets must be configured for {spec_id} — noNewSecrets=false"
                fp = compute_fingerprint(spec_id, text)
                new_actions.append(OpsAction(
                    id=_next_id(existing_ids + [a.id for a in new_actions]),
                    spec_id=spec_id,
                    title=f"Wrangler secrets needed for {spec_id}",
                    description=text,
                    category="secret",
                    priority="critical",
                    source_agent=source_agent,
                    source_sequence=sequence,
                    fingerprint=fp,
                    auto_extracted=True,
                ))

        # 3. nextAgent targeting human
        next_agent = raw.get("nextAgent", {})
        if isinstance(next_agent, dict) and next_agent.get("target") == "human":
            action_text = next_agent.get("action", "")
            if isinstance(action_text, str) and len(action_text.strip()) > 10 and self._is_ops_action(action_text):
                fp = compute_fingerprint(spec_id, action_text)
                new_actions.append(OpsAction(
                    id=_next_id(existing_ids + [a.id for a in new_actions]),
                    spec_id=spec_id,
                    title=action_text.strip()[:80],
                    description=action_text.strip(),
                    category=classify_category(action_text),
                    priority=classify_priority(action_text),
                    source_agent=source_agent,
                    source_sequence=sequence,
                    fingerprint=fp,
                    auto_extracted=True,
                ))

        # Add new actions (dedup handled by register)
        added = 0
        for action in new_actions:
            if self.ops_register.add(action):
                added += 1
                # Notify for critical/warning actions
                if action.effective_priority == "critical":
                    self._notify(
                        f"⛔ OPS ACTION REQUIRED [{spec_id}]: {action.title}",
                        critical=True,
                    )
                elif action.effective_priority == "warning":
                    self._notify(
                        f"⚠️ OPS ACTION [{spec_id}]: {action.title}",
                    )

        if added:
            self.ops_register.save()
            log.info(f"Ops extraction: {added} new action(s) from {handoff.spec_id} #{sequence}")

        # 4. Stale detection: check if existing pending actions for this spec
        # are no longer mentioned in current handoff's unresolvedIssues
        if current_texts:
            for action in self.ops_register.get_pending_for_spec(spec_id):
                if not action.auto_extracted:
                    continue
                # Check if the action's description (normalized) still appears
                desc_lower = action.description.lower().strip()
                still_present = any(
                    desc_lower in t or t in desc_lower
                    for t in current_texts
                )
                if not still_present and not action.stale_since:
                    self.ops_register.mark_stale(action.id)
                    log.info(f"Ops stale: {action.id} ({action.title[:50]}) no longer in {spec_id} handoff")
                elif still_present and action.stale_since:
                    self.ops_register.clear_stale(action.id)
                    log.info(f"Ops un-stale: {action.id} reappeared in {spec_id}")

            # Save if any stale changes
            self.ops_register.save()

    # ------------------------------------------------------------------
    # Ops Agent — reactive/proactive triggers and completion
    # ------------------------------------------------------------------

    def _schedule_resolve_ops(self, context: str = ""):
        """Create a resolve-ops directive programmatically.

        Called reactively when Planner is blocked by ops issues,
        or proactively when critical actions have been pending too long.
        """
        if not self.config.ops_agent_enabled:
            return

        # Don't schedule if gate is already pending or agent is busy
        if self.directive_processor._ops_gate_pending:
            log.debug("_schedule_resolve_ops: gate already pending, skipping")
            return

        if not self.commander.check_agent_007_idle():
            log.debug("_schedule_resolve_ops: Agent 007 busy, skipping")
            return

        # Create synthetic directive data (bypass filesystem)
        directive_data = {
            "directive": "resolve-ops",
            "filter": "all",
            "context": context or "Programmatic trigger",
        }

        log.info(f"_schedule_resolve_ops: triggering resolve-ops ({context})")
        self.directive_processor._handle_resolve_ops(directive_data)

    def _check_ops_proactive(self):
        """Proactive ops resolution — fires when critical actions pending too long.

        Called every 60s from run_loop. Checks if any critical ops actions
        have been pending longer than ops_proactive_threshold_minutes.
        """
        if not self.config.ops_agent_enabled:
            return

        now = time.time()
        # Throttle to once per 60 seconds
        if now - self._last_ops_proactive_check < 60:
            return
        self._last_ops_proactive_check = now

        # Reload register
        self.ops_register.load()
        if not self.ops_register.has_pending_critical():
            return

        # Check if any critical actions exceed threshold
        from .ops_actions import OpsActionPriority
        critical = self.ops_register.get_pending(priority=OpsActionPriority.CRITICAL)
        if not critical:
            return

        threshold_seconds = self.config.ops_proactive_threshold_minutes * 60
        oldest_age = max(
            (now - datetime.fromisoformat(a.created_at).timestamp())
            for a in critical
            if a.created_at
        )

        if oldest_age >= threshold_seconds:
            age_minutes = int(oldest_age / 60)
            self._schedule_resolve_ops(
                f"Proactive: {len(critical)} critical ops actions pending "
                f"({age_minutes}m oldest, threshold={self.config.ops_proactive_threshold_minutes}m)"
            )

    def _handle_ops_completion(self, handoff: Handoff):
        """Handle completion of an ops agent (Agent 007) run.

        Called in process_handoff when handoff comes from Agent 007.
        Reloads register and logs summary of what was resolved.
        """
        self.ops_register.load()

        # Count resolved/skipped/pending
        summary = self.ops_register.get_summary()
        completed = summary.get("completed", 0)
        skipped = summary.get("skipped", 0)
        pending = summary.get("pending", 0)

        outcome = handoff.status.outcome if handoff.status else "unknown"
        msg = (
            f"Ops agent completed ({outcome}): "
            f"{completed} resolved, {skipped} skipped, {pending} still pending"
        )
        log.info(msg)
        self._notify(f"🔧 {msg}")

    def _handle_bug_completion(self, handoff: Handoff):
        """Handle completion of a bug-fix pipeline (BUG-FIX-* spec).

        Called in process_handoff step 15 when a BUG-FIX-* spec reaches
        a terminal target (human/none). Updates GitHub labels, posts a
        summary comment, sends notification, and advances the bug queue.
        """
        import urllib.request
        import urllib.error

        spec_id = handoff.spec_id
        try:
            issue_num = int(spec_id.replace("BUG-FIX-", ""))
        except ValueError:
            log.warning(f"Could not parse issue number from {spec_id}")
            return

        outcome = handoff.status.outcome if handoff.status else "unknown"
        source = handoff.source_agent

        # Retrieve GitHub token from directive processor (set during resolve-bugs)
        token = self.directive_processor._bugs_github_token
        if not token:
            # Try to fetch fresh token
            try:
                result = subprocess.run(
                    [
                        "python3",
                        str(self.config.skills_dir / "credential-manager" / "get_credential.py"),
                        "github", "--field", "secret", "--quiet",
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                token = result.stdout.strip()
            except Exception as e:
                log.warning(f"Bug completion: could not retrieve GitHub PAT: {e}")

        repo = self.config.bugs_github_repo

        if token:
            # Update GitHub label: in-progress → verify
            self.directive_processor._bugs_github_token = token
            bug_stub = {"number": issue_num}
            self.directive_processor._update_github_label(
                bug_stub,
                remove_label=self.config.bugs_label_in_progress,
                add_label=self.config.bugs_label_verify,
            )

            # Post summary comment on the issue
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                comment_body = (
                    f"Fix deployed to production via IWO pipeline.\n"
                    f"Spec: {spec_id} | Agents: Planner → Builder → Reviewer → "
                    f"Tester → Deployer → Docs\n"
                    f"Final agent: {source} | Outcome: {outcome}\n"
                    f"Awaiting human verification. — IWO [{ts}]"
                )
                url = f"https://api.github.com/repos/{repo}/issues/{issue_num}/comments"
                body = json.dumps({"body": comment_body}).encode()
                req = urllib.request.Request(url, data=body, method="POST", headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "IWO-resolve-bugs/1.0",
                    "X-GitHub-Api-Version": "2022-11-28",
                })
                urllib.request.urlopen(req, timeout=15)
                log.info(f"Bug completion: posted comment on #{issue_num}")
            except Exception as e:
                log.warning(f"Bug completion: failed to post comment on #{issue_num}: {e}")

        # Send ntfy notification
        self._notify(
            f"🐛 {spec_id} deployed — verify on production (#{issue_num})",
            critical=True,
        )

        log.info(f"Bug completion: {spec_id} ({outcome}) — advancing queue")

        # Advance to next bug in queue
        self.directive_processor._advance_bug_queue()

    def process_handoff(self, path: Path):
        """Parse, validate, and route a handoff file.

        Phase 2.3: Pipeline-aware routing with per-agent queuing and
        rejection-first priority.
        """
        # 1. Parse and validate
        try:
            with open(path) as f:
                data = json.load(f)
            handoff = Handoff(**data)
        except json.JSONDecodeError as e:
            log.error(f"Invalid JSON in {path.name}: {e}")
            self._notify(f"Invalid JSON: {path.name}", critical=True)
            return
        except ValidationError as e:
            log.error(f"Handoff validation failed for {path.name}: {e}")
            self._notify(f"Invalid handoff structure: {path.name}", critical=True)
            return

        log.info(
            f"Handoff #{handoff.sequence}: "
            f"{handoff.source_agent} → {handoff.target_agent} "
            f"[{handoff.status.outcome}] ({handoff.spec_id})"
        )

        # 2. Idempotency check FIRST (before validation) — catches legitimate
        # retries and supersedes before source agent validation would
        # false-positive on them.  See review finding M3: after record_handoff
        # flips current_agent to target, a retry from the source agent would
        # fail source validation despite being a harmless duplicate.
        if self.tracker.already_processed(handoff, path):
            log.info(f"Already processed {handoff.idempotency_key}, skipping")
            return

        # ── Phase 2 fix: Validate handoffs on ingestion ──────────────
        # Reject rogue writes by checking source agent assignment and
        # sequence continuity.  Quarantine invalid files so they don't
        # pollute future reconciliation scans.  See incident 2026-04-11.

        # 2a. Source agent validation: handoff must come from the agent
        # currently assigned to this spec in PipelineManager.
        pipeline_for_val = self.pipeline.get_pipeline(handoff.spec_id)
        if pipeline_for_val and pipeline_for_val.current_agent:
            expected_agent = pipeline_for_val.current_agent
            if handoff.source_agent != expected_agent:
                log.critical(
                    f"DESYNC: {path.name} claims source={handoff.source_agent} "
                    f"but PipelineManager expects {expected_agent} for "
                    f"{handoff.spec_id}. Possible rogue write — quarantining."
                )
                self._notify(
                    f"🚨 DESYNC: {handoff.source_agent} wrote handoff for "
                    f"{handoff.spec_id} but {expected_agent} is assigned. "
                    f"File quarantined: {path.name}",
                    critical=True,
                )
                self._quarantine_file(path, "source_agent_mismatch")
                return

        # 2b. Sequence validation: handoff sequence should be
        # last_processed + 1.  Out-of-order files indicate corruption
        # or duplicate writes.  We allow sequence == handoff_count + 1
        # (next expected) or sequence <= handoff_count (already processed,
        # caught by idempotency check above).
        if pipeline_for_val:
            expected_seq = pipeline_for_val.handoff_count + 1
            if handoff.sequence > expected_seq:
                log.critical(
                    f"SEQUENCE GAP: {path.name} has seq={handoff.sequence} "
                    f"but expected {expected_seq} for {handoff.spec_id}. "
                    f"Missing handoff(s) — quarantining."
                )
                self._notify(
                    f"🚨 SEQUENCE GAP: {path.name} seq={handoff.sequence}, "
                    f"expected {expected_seq}. Quarantined.",
                    critical=True,
                )
                self._quarantine_file(path, "sequence_gap")
                return

        # Clear stall watchdog — this agent successfully wrote its handoff
        self._stall_watchdog.pop(handoff.source_agent, None)
        self._stall_alert_sent.discard(handoff.source_agent)
        # Phase 5: Clear the once-per-stall guard so next stall can fire
        self._auto_recovery_fired.discard(
            (handoff.source_agent, handoff.spec_id)
        )

        # 3. Safety rails
        if self.tracker.check_handoff_limit(handoff, self.config.max_handoffs_per_spec):
            msg = f"HALT: {handoff.spec_id} exceeded {self.config.max_handoffs_per_spec} handoffs"
            log.error(msg)
            self._notify(msg, critical=True)
            self.pipeline.mark_halted(handoff.spec_id, "handoff limit exceeded")
            self._clear_auto_recovery_state(handoff.spec_id)
            return

        if self.tracker.check_rejection_loop(handoff, self.config.max_rejection_loops):
            msg = f"HALT: Rejection loop detected in {handoff.spec_id}"
            log.error(msg)
            self._notify(msg, critical=True)
            self.pipeline.mark_halted(handoff.spec_id, "rejection loop")
            self._clear_auto_recovery_state(handoff.spec_id)
            return

        # 4. Mark processed and record in history
        self.tracker.mark_processed(handoff, path)
        self.handoff_history.insert(0, handoff)
        if len(self.handoff_history) > self._max_history:
            self.handoff_history.pop()

        # 5. Pipeline bookkeeping: update spec pipeline, release source agent
        self.pipeline.record_handoff(handoff)

        # 5.1 Store to memory (best-effort, non-blocking)
        if self.memory:
            try:
                proc_start = time.monotonic()
                self.memory.store_handoff(handoff, processing_time_ms=0)
                proc_ms = (time.monotonic() - proc_start) * 1000
                log.info(f"Memory: stored handoff in {proc_ms:.0f}ms")
            except Exception as e:
                log.warning(f"Memory: store failed (non-fatal): {e}")

        # 6. Update LATEST.json symlink
        self._update_latest(path, handoff)

        # 6.1 Stamp canonical received_at time (agents fabricate timestamps)
        try:
            with open(path) as f:
                raw = json.load(f)
            raw.setdefault("metadata", {})["received_at"] = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            )
            with open(path, "w") as f:
                json.dump(raw, f, indent=2)
        except Exception as e:
            log.warning(f"Could not stamp received_at on {path.name}: {e}")

        # 7. Write .active-specs.json for external visibility
        self._write_active_specs()

        # 7.1 Post-deploy health check (Phase 2.4.2)
        if (handoff.source_agent == "deployer"
                and handoff.status.outcome == "success"
                and self.config.health_check_urls):
            self._run_post_deploy_health_check(handoff)

        # 8. Human gate check (conditional: auto-approve if no infrastructure changes)
        target = handoff.target_agent
        if target in self.config.human_gate_agents:
            # Auto-deploy-all: bypass gate entirely
            if self.config.auto_deploy_all:
                log.info(
                    f"Deploy auto-approved for {handoff.spec_id}: "
                    f"auto_deploy_all enabled (gate bypassed)"
                )
                self._notify(
                    f"🚀 AUTO-DEPLOY (all): {handoff.spec_id} → {target}"
                )
                # Fall through to step 9 routing
            else:
                approved, reason = self._should_auto_approve_deploy(path, handoff)
                if approved:
                    log.info(
                        f"Deploy auto-approved for {handoff.spec_id}: {reason}"
                    )
                    self._notify(
                        f"✅ AUTO-DEPLOY: {handoff.spec_id} → {target} "
                        f"({reason})"
                    )
                    # Fall through to step 9 routing
                else:
                    # Build pending critical ops actions list for this spec
                    ops_detail = ""
                    pending_critical = self.ops_register.get_pending_for_spec(
                        handoff.spec_id, "critical"
                    )
                    if pending_critical:
                        ops_lines = [f"  - {a.title}" for a in pending_critical[:5]]
                        ops_detail = (
                            f"\n⛔ {len(pending_critical)} pending critical ops action(s):\n"
                            + "\n".join(ops_lines)
                        )
                    msg = (
                        f"🚦 DEPLOY GATE: {handoff.spec_id} ready for {target}. "
                        f"Reason: {reason}. "
                        f"Press 'd' to approve. "
                        f"Action: {handoff.nextAgent.action[:100]}"
                        f"{ops_detail}"
                    )
                    log.info(msg)
                    self._notify(msg, critical=True)
                    # Append to pending queue (FIFO — 'd' key approves oldest first)
                    self._deploy_gate_pending.append((handoff, path))
                    return

        # 8.5 Terminal targets — pipeline complete, no activation needed
        if target in ("human", "none"):
            self.pipeline.mark_completed(handoff.spec_id)
            self._clear_auto_recovery_state(handoff.spec_id)
            self._write_active_specs()
            self._notify(
                f"🏁 {handoff.spec_id} → {target} (pipeline complete, "
                f"{handoff.source_agent} was final agent)"
            )
            log.info(
                f"Pipeline complete: {handoff.spec_id} → {target} "
                f"(terminal target, no activation)"
            )

            # 8.6 Bug-fix pipeline completion (before return)
            if handoff.spec_id.startswith("BUG-FIX-"):
                try:
                    self._handle_bug_completion(handoff)
                except Exception as e:
                    log.warning(f"Bug completion handler failed (non-fatal): {e}")

            # Auto-continue: queue next-spec directive if enabled
            if (
                self.config.auto_continue_on_completion
                and handoff.status.outcome == "success"
            ):
                self._schedule_auto_continue(handoff.spec_id)
            return

        # 9. Route to target agent — deterministic idle check
        #    Headless Phase 2: pane_current_command check replaces canary probes.
        if target not in self.agent_states:
            log.warning(f"No agent pane for {target}, queuing {handoff.spec_id}")
            self.pipeline.enqueue(handoff, path)
            self._notify(f"⏸️ Queued {handoff.spec_id} → {target} (no pane found)")
        else:
            # Release any stale assignment on the target agent before dispatching
            stale_spec = self.pipeline.agent_current_spec(target)
            if stale_spec and stale_spec != handoff.spec_id:
                # Bug 2 fix: never pre-empt real work for phantom/auto-generated handoffs
                is_auto = getattr(handoff.metadata, 'auto_generated', False) if handoff.metadata else False
                if handoff.spec_id == "unknown" or is_auto:
                    log.warning(
                        f"Refusing to pre-empt {target}/{stale_spec} for "
                        f"auto-generated {handoff.spec_id} — queuing instead"
                    )
                    self.pipeline.enqueue(handoff, path)
                    return
                log.info(
                    f"Releasing stale assignment: {target} was on {stale_spec}, "
                    f"now dispatching {handoff.spec_id}"
                )
                self.pipeline.release_agent(target)

            # Deterministic idle check — pane_current_command ∈ IDLE_SHELLS
            if self.commander.is_agent_idle(target):
                log.info(f"Agent {target} idle — dispatching immediately")
                self._activate_for_handoff(target, handoff, path)
            else:
                # Agent is busy — queue for retry via _process_pending_activations
                log.info(
                    f"Agent {target} busy — queuing {handoff.spec_id} "
                    f"(will dispatch when idle)"
                )
                self.pipeline.enqueue(handoff, path)

        # 10. After processing, check if the released source agent has queued work
        #     Headless Phase 2: deterministic idle check replaces canary
        source = handoff.source_agent
        if (source in self.agent_states
                and not self.pipeline.is_agent_busy(source)
                and self.commander.is_agent_idle(source)):
            queued = self.pipeline.dequeue(source)
            if queued:
                log.info(f"Queue drain: {source} freed, activating queued work")
                self._activate_for_handoff(source, queued.handoff, queued.path)

        # 11. Auditor: post-handoff invariant checks (best-effort)
        if self.auditor:
            try:
                self.auditor.post_handoff_checks(handoff)
            except Exception as e:
                log.warning(f"Auditor post-handoff check failed (non-fatal): {e}")

        # 12. Ops Actions: extract human-required tasks from handoff (best-effort)
        try:
            self._extract_ops_actions(handoff, path)
        except Exception as e:
            log.warning(f"Ops actions extraction failed (non-fatal): {e}")

        # 13. Ops Agent: handle completion if handoff is from Agent 007
        if source == "agent-007" or (hasattr(handoff.metadata, 'agent') and handoff.metadata.agent == "agent-007"):
            try:
                self._handle_ops_completion(handoff)
            except Exception as e:
                log.warning(f"Ops completion handler failed (non-fatal): {e}")

        # 14. Reactive ops trigger: if Planner blocked by unresolved ops issues
        if target == "human" and handoff.status and handoff.status.outcome in ("blocked", "failed"):
            unresolved = getattr(handoff.status, 'unresolvedIssues', []) or []
            ops_keywords = ["migration", "wrangler", "secret", "r2 bucket", "dns", "not yet"]
            if any(kw in issue.lower() for issue in unresolved for kw in ops_keywords):
                try:
                    self._schedule_resolve_ops(
                        f"Reactive: {source} blocked on ops issues for {spec_id}"
                    )
                except Exception as e:
                    log.warning(f"Reactive ops schedule failed (non-fatal): {e}")

        # (Step 15 removed — bug completion handled in step 8.6 before terminal return)

    def _reconcile_filesystem(self):
        """Periodic scan to catch missed inotify events. Called every 30s.

        Phase 2.3: Scans ALL spec subdirectories, not just .current-spec.
        """
        if not self.config.handoffs_dir.exists():
            return

        # Scan all subdirectories that look like spec dirs
        for spec_dir in sorted(self.config.handoffs_dir.iterdir()):
            if not spec_dir.is_dir():
                continue
            if spec_dir.name.startswith("."):
                continue  # Skip .current-spec etc.

            # Skip specs whose pipeline is already completed/stale in
            # PipelineManager.  We no longer read LATEST.json here — it is
            # agent-writable and can be corrupted by rogue files.  The
            # authoritative state is PipelineManager (control plane), not
            # the filesystem (data plane).  See incident 2026-04-11.
            pipeline = self.pipeline.get_pipeline(spec_dir.name)
            if pipeline and pipeline.status in ("completed", "stale", "halted"):
                # ── Phase 6 fix 6c: Fast-path skip telemetry ──────────
                log.debug(
                    f"Reconciliation: skipping {spec_dir.name} "
                    f"(pipeline status={pipeline.status})"
                )
                continue

            json_files = sorted(spec_dir.glob("*.json"))
            json_files = [
                f for f in json_files
                if f.name != "LATEST.json"
                and not f.name.endswith(".tmp")
            ]

            missed = 0
            for f in json_files:
                try:
                    # ── Phase 4 fix 4a: Resolve symlinks ──────────────
                    f = f.resolve()
                    # ── Phase 4 fix 4b: File stabilization ────────────
                    # Nextcloud syncs files incrementally — a half-written
                    # file will have a recent mtime.  Skip files whose
                    # mtime changed in the last 1s to avoid reading
                    # partial writes.  They'll be caught on the next
                    # reconciliation cycle (30s).
                    try:
                        mtime = f.stat().st_mtime
                        if time.time() - mtime < 1.0:
                            log.debug(f"Reconciliation: skipping {f.name} (mtime unstable)")
                            continue
                    except OSError:
                        continue  # File disappeared between glob and stat
                    with open(f) as fh:
                        data = json.load(fh)
                    handoff = Handoff(**data)
                    if not self.tracker.already_processed(handoff):
                        # ── Phase 4 fix 4c: Detection layer attribution ──
                        log.info(
                            f"Reconciliation: found missed handoff {f.name} "
                            f"(inotify missed — reconciliation layer catch)"
                        )
                        self.process_handoff(f)
                        missed += 1
                except Exception:
                    pass

            if missed > 0:
                # ── Phase 4 fix 4c: Alert if reconciliation is consistently
                # catching files that inotify should have caught
                log.warning(
                    f"Reconciliation: processed {missed} missed handoff(s) "
                    f"for {spec_dir.name} — inotify may be unreliable"
                )

    def _clear_auto_recovery_state(self, spec_id: str):
        """Reset Phase 5 auto-recovery cascade guard state for a spec.

        Called when a pipeline reaches a terminal state (completed/halted)
        so that a future sprint on the same spec starts with fresh counters.
        Without this, stale counts/cooldowns from Sprint N block legitimate
        auto-recovery on Sprint N+1.  See review finding H1/M4.
        """
        self._auto_recovery_count.pop(spec_id, None)
        self._auto_recovery_last.pop(spec_id, None)
        # Clear any fired guards for this spec (across all agents)
        self._auto_recovery_fired = {
            (agent, sid) for agent, sid in self._auto_recovery_fired
            if sid != spec_id
        }

    def _quarantine_file(self, path: Path, reason: str):
        """Move a rogue/invalid handoff file to .quarantine/ subdirectory.

        Phase 2 fix: quarantine files that fail ingestion validation
        (source agent mismatch, sequence gap) so they don't pollute
        future reconciliation scans.  See incident 2026-04-11.
        """
        quarantine_dir = path.parent / ".quarantine"
        try:
            quarantine_dir.mkdir(exist_ok=True)
            dest = quarantine_dir / f"{reason}__{path.name}"
            path.rename(dest)
            log.warning(f"Quarantined {path.name} → .quarantine/{dest.name}")
        except Exception as e:
            log.error(f"Failed to quarantine {path.name}: {e}")

    def _update_latest(self, handoff_path: Path, handoff: Handoff):
        """Update LATEST.json as a symlink to the current handoff."""
        spec_dir = handoff_path.parent
        latest = spec_dir / "LATEST.json"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(handoff_path.name)
            log.info(f"LATEST.json → {handoff_path.name}")
        except Exception as e:
            log.warning(f"Failed to update LATEST.json: {e}")

    def _schedule_auto_continue(self, completed_spec_id: str):
        """Queue a next-spec directive after a pipeline completes successfully.

        Creates a directive JSON in .directives/ so the normal directive processing
        loop picks it up. Uses a delay to let file writes settle.

        Only fires if:
        - auto_continue_on_completion is True (already checked by caller)
        - No other active pipelines (avoid overloading agents)
        - Planner pane is idle
        """
        # Guard: don't auto-continue if other pipelines are active
        active_count = self.pipeline.active_count
        if active_count > 0:
            log.info(
                f"Auto-continue skipped: {active_count} active pipeline(s) remain"
            )
            return

        # Guard: check planner is idle
        from .state import AgentState
        planner_state = self.agent_states.get("planner", AgentState.UNKNOWN)
        if planner_state not in (AgentState.IDLE, AgentState.UNKNOWN):
            log.info(
                f"Auto-continue skipped: planner is {planner_state.value}"
            )
            return

        # Write the directive file after a short delay
        def _write_directive():
            time.sleep(self.config.auto_continue_delay_seconds)
            try:
                directives_dir = self.config.handoffs_dir / ".directives"
                directives_dir.mkdir(parents=True, exist_ok=True)
                ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                ts_ns = time.time_ns()

                # Check for remaining sprints before deciding directive type
                sprint_info = self._detect_remaining_sprints(completed_spec_id)

                if sprint_info:
                    # Multi-sprint continuation: issue start-spec for same specId
                    filename = f"{ts_ns}-auto-continue-sprint.json"
                    directive = {
                        "directive": "start-spec",
                        "specId": completed_spec_id,
                        "sprintContext": sprint_info,
                        "timestamp": ts_iso,
                        "auto_generated": True,
                    }
                    log.info(
                        f"Auto-continue: sprint continuation for {completed_spec_id} "
                        f"Sprint {sprint_info['next_sprint']}/{sprint_info['total_sprints']}"
                    )
                else:
                    # Single-sprint or final sprint: existing behaviour
                    filename = f"{ts_ns}-auto-next-spec.json"
                    directive = {
                        "directive": "next-spec",
                        "focus": (
                            f"Auto-continue after {completed_spec_id} completed "
                            f"successfully. Select the next logical spec."
                        ),
                        "timestamp": ts_iso,
                        "auto_generated": True,
                    }

                directive_path = directives_dir / filename
                directive_path.write_text(json.dumps(directive))
                log.info(
                    f"Auto-continue: queued {directive['directive']} directive "
                    f"→ {directive_path.name}"
                )
                self._notify(
                    f"🔄 Auto-continue: {directive['directive']} queued "
                    f"after {completed_spec_id}"
                )
            except Exception as e:
                log.error(f"Auto-continue failed to write directive: {e}")

        thread = threading.Thread(target=_write_directive, daemon=True)
        thread.start()

    def _detect_remaining_sprints(self, spec_id: str) -> Optional[dict]:
        """Detect if a spec has remaining sprints by parsing its implementation plan.

        Returns dict with sprint info if more sprints remain, None otherwise.
        Format: {"next_sprint": int, "total_sprints": int, "plan_path": str}
        """
        # Locate implementation plan
        plan_patterns = [
            self.config.project_root / "docs" / "plans" / f"{spec_id}-implementation-plan.md",
            self.config.project_root / "docs" / "plans" / f"{spec_id.lower()}-implementation-plan.md",
        ]

        plan_path = None
        for p in plan_patterns:
            if p.exists():
                plan_path = p
                break

        if not plan_path:
            log.info(f"No implementation plan found for {spec_id} — treating as single-sprint")
            return None

        # Parse sprint/phase headers
        try:
            content = plan_path.read_text()
        except Exception as e:
            log.warning(f"Failed to read plan for {spec_id}: {e}")
            return None

        # Match both "### Sprint N" and "### Phase N (Sprint N)" patterns
        sprint_pattern = re.compile(
            r'^###\s+(?:Sprint\s+(\d+)|Phase\s+\d+\s*\(?\s*Sprint\s+(\d+)\s*\)?)',
            re.MULTILINE | re.IGNORECASE
        )
        matches = sprint_pattern.findall(content)

        if not matches:
            log.info(f"No sprint headers found in plan for {spec_id}")
            return None

        # Extract sprint numbers (either group 1 or group 2 will match)
        sprint_numbers = sorted(set(
            int(m[0] or m[1]) for m in matches
        ))
        total_sprints = max(sprint_numbers)

        if total_sprints <= 1:
            return None  # Single-sprint spec

        # Count completed sprint pipelines from handoff history
        comms_dir = self.config.handoffs_dir / spec_id
        if not comms_dir.exists():
            log.warning(f"No agent-comms directory for {spec_id}")
            return None

        # Count distinct completed pipeline runs (each ends with a docs-agent handoff)
        completed_runs = 0
        for h in sorted(comms_dir.glob("*-docs-*.json")):
            try:
                data = json.loads(h.read_text())
                target = data.get("nextAgent", {}).get("target", "")
                outcome = data.get("status", {}).get("outcome", "")
                if target in ("human", "none") and outcome == "success":
                    completed_runs += 1
            except Exception:
                continue

        if completed_runs >= total_sprints:
            log.info(f"{spec_id}: all {total_sprints} sprints completed")
            return None

        next_sprint = completed_runs + 1
        log.info(
            f"{spec_id}: sprint {completed_runs}/{total_sprints} completed, "
            f"next sprint: {next_sprint}"
        )

        return {
            "next_sprint": next_sprint,
            "total_sprints": total_sprints,
            "plan_path": str(plan_path.relative_to(self.config.project_root)),
        }

    def _write_active_specs(self):
        """Write .active-specs.json for external visibility (TUI, other tools).

        Also maintains .current-spec for backward compatibility (set to most recent active spec).
        """
        try:
            specs_file = self.config.handoffs_dir / ".active-specs.json"
            state = self.pipeline.to_dict()
            # ── Phase 6 fix 6b: Surface watchdog state ────────────
            now = time.time()
            state["watchdog"] = {
                agent: {
                    "spec_id": spec_id,
                    "idle_seconds": int(now - idle_at),
                    "alert_sent": agent in self._stall_alert_sent,
                }
                for agent, (idle_at, spec_id) in self._stall_watchdog.items()
            }
            with open(specs_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write .active-specs.json: {e}")

        # Backward compat: .current-spec = most recently active spec
        try:
            active = [
                p for p in self.pipeline.all_pipelines if p.status == "active"
            ]
            if active:
                current_spec_file = self.config.handoffs_dir / ".current-spec"
                current_spec_file.write_text(active[0].spec_id)
        except Exception:
            pass

    def _notify(self, message: str, critical: bool = False):
        """Send notification via configured channels."""
        channels = self.config.notification_channels

        if "ntfy" in channels:
            self._notify_ntfy(message, critical)

        if "desktop" in channels:
            self._notify_desktop(message, critical)

        if "webhook" in channels:
            self._notify_webhook(message, critical)

    def _notify_ntfy(self, message: str, critical: bool = False):
        """Send push notification via ntfy (mobile phone).

        ntfy is a simple HTTP-based pub/sub notification service.
        Subscribe to the topic in the ntfy Android/iOS app to receive
        all IWO notifications on your phone.
        """
        from urllib.request import Request, urlopen
        from urllib.error import URLError

        url = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        priority = (
            self.config.ntfy_priority_critical if critical
            else self.config.ntfy_priority_normal
        )

        # Determine a short tag/emoji for the notification
        if "OPS ACTION REQUIRED" in message:
            tags = "rotating_light"
        elif "OPS ACTION" in message and critical:
            tags = "rotating_light"
        elif "OPS ACTION" in message:
            tags = "warning"
        elif "AUTO-DEPLOY" in message or "activated" in message:
            tags = "rocket"
        elif "DEPLOY GATE" in message:
            tags = "construction"
        elif "FAIL" in message.upper() or "CRASH" in message.upper():
            tags = "warning"
        elif "STALE" in message.upper():
            tags = "snail"
        else:
            tags = "robot"

        # Extract a short title from the message (first ~50 chars)
        # Strip non-ASCII to avoid latin-1 encoding errors in HTTP headers
        title_raw = message[:60].split(".")[0].split("→")[0].strip()
        title = title_raw.encode("ascii", errors="ignore").decode("ascii").strip()

        req = Request(url, data=message.encode("utf-8"))
        req.add_header("Title", f"IWO: {title}")
        req.add_header("Priority", str(priority))
        req.add_header("Tags", tags)

        try:
            with urlopen(req, timeout=self.config.ntfy_timeout) as resp:
                log.debug(f"ntfy notification sent: {resp.status}")
        except URLError as e:
            log.warning(f"ntfy notification failed: {e}")
        except Exception as e:
            log.warning(f"ntfy notification error: {e}")

    def _notify_desktop(self, message: str, critical: bool = False):
        """Send desktop notification via notify-send."""
        urgency = "critical" if critical else "normal"
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "IWO", "Ivan's Workflow", message],
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            log.warning(f"notify-send failed: {e}")

    def _notify_webhook(self, message: str, critical: bool = False):
        """Send notification via webhook (e.g., n8n) as JSON POST."""
        url = self.config.notification_webhook_url
        if not url:
            log.debug("Webhook notification skipped: no URL configured")
            return

        import json
        import time as _time
        from urllib.request import Request, urlopen
        from urllib.error import URLError

        # Build context-rich payload for n8n processing
        active_specs = [p.spec_id for p in self.pipeline.get_active()]
        payload = json.dumps({
            "source": "iwo",
            "message": message,
            "critical": critical,
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "active_specs": active_specs,
            "version": "2.5.2",
        }).encode("utf-8")

        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.config.notification_webhook_timeout) as resp:
                log.debug(f"Webhook notification sent: {resp.status}")
        except URLError as e:
            log.warning(f"Webhook notification failed: {e}")
        except Exception as e:
            log.warning(f"Webhook notification error: {e}")

    def setup(self) -> bool:
        """Initialize daemon: connect to tmux, set up state machines, recover state, start watcher.

        Returns True if setup succeeded. Called by both headless start() and TUI mode.
        Does NOT enter the main loop — call run_loop() for headless or let TUI drive polling.
        """
        log.info("=" * 60)
        log.info("IWO — Ivan's Workflow Orchestrator v1.0")
        log.info("Phase 2: Headless Dispatch + Deterministic Idle Detection")
        log.info("=" * 60)

        # 1. Connect to tmux and discover agents (with tag-based discovery)
        if not self.commander.connect():
            log.error("Cannot connect to tmux session. Is the workflow running?")
            return False

        # 2. Set up agent environments (pipe-pane archival)
        self.commander.setup_agent_environments()

        # 3. Initialize agent state tracking for all discovered agents
        self._init_agent_states()

        # 4. Scan for current state (stateless recovery)
        self._recover_state()

        # 5. Start filesystem watcher
        handler = HandoffHandler(self)
        self.observer = Observer()
        watch_path = str(self.config.handoffs_dir)
        self.observer.schedule(handler, watch_path, recursive=True)
        self.observer.start()
        log.info(f"Watching: {watch_path}")

        # 6. Initialize memory integration (best-effort)
        if self.config.enable_memory:
            self.memory = IWOMemory(self.config)
            if self.memory.initialize():
                log.info("Memory integration active")
            else:
                log.warning("Memory integration unavailable — continuing without persistence")

        # 7. Initialize metrics collector (uses memory's Neo4j connection)
        self.metrics = MetricsCollector(self.memory)
        log.info("Metrics collector initialized")

        # 8. Initialize auditor module (Agent 007 Phase 1)
        try:
            self.auditor = Auditor(self, AuditorConfig(
                webhook_url=self.config.notification_webhook_url,
            ))
            log.info("Auditor module initialized")

            # Check if Agent 007 is already running on startup
            if not self.commander.check_agent_007_idle():
                self.auditor._007_active = True
                log.info("Agent 007 appears active on startup — marking as running")
        except Exception as e:
            log.warning(f"Auditor initialization failed (non-fatal): {e}")
            self.auditor = None

        self._notify("IWO v1.0 started — state machine active")

        # 9. Initialize directive processor directories
        self.directive_processor.ensure_dirs()
        log.info(f"Directive processor active: {self.directive_processor.directives_dir}")
        return True

    def run_loop(self):
        """Headless main loop: state polling + reconciliation on tick counters.

        For TUI mode, the Textual app drives polling via set_interval instead.
        """
        poll_every = max(1, int(self.config.state_poll_interval_seconds))
        recon_every = self.config.reconciliation_interval_seconds
        tick = 0

        try:
            while True:
                time.sleep(1)
                tick += 1

                if tick % poll_every == 0:
                    self._poll_agent_states()

                if tick % recon_every == 0:
                    self._reconcile_filesystem()

                # Poll for operator directives every 2 seconds
                if tick % poll_every == 0:
                    self.directive_processor.poll()

                # Proactive ops check every 60 seconds
                if tick % 60 == 0:
                    try:
                        self._check_ops_proactive()
                    except Exception as e:
                        log.warning(f"Proactive ops check failed (non-fatal): {e}")

                # Auditor periodic checks (self-throttles to 5-min intervals)
                if self.auditor:
                    try:
                        self.auditor.periodic_checks()
                        completion = self.auditor.check_007_completion()
                        if completion:
                            log.info(f"Agent 007 completed: {completion.get('outcome')}")
                    except Exception as e:
                        log.warning(f"Auditor check failed (non-fatal): {e}")

        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.observer.stop()
        self.observer.join()
        log.info("IWO stopped.")

    def shutdown(self):
        """Clean shutdown — stop observer, close memory, and log."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.memory:
            self.memory.close()
        log.info("IWO stopped.")

    def start(self):
        """Start the daemon: connect to tmux, init state machines, watch for handoffs."""
        if not self.setup():
            sys.exit(1)
        self.run_loop()

    def _recover_state(self):
        """Scan filesystem to reconstruct state after restart.

        Phase 2.3: Scans ALL spec directories and rebuilds pipeline state.
        """
        if not self.config.handoffs_dir.exists():
            log.info("No handoffs directory found")
            return

        total_specs = 0
        total_handoffs = 0

        for spec_dir in sorted(self.config.handoffs_dir.iterdir()):
            if not spec_dir.is_dir():
                continue
            if spec_dir.name.startswith("."):
                continue

            # Gather actual handoff files (skip LATEST.json symlink — it is
            # agent-writable and cannot be trusted for control-plane decisions).
            # We determine terminal status from the highest-sequence handoff
            # file content instead.  See incident 2026-04-11.
            spec_id = spec_dir.name
            json_files = sorted(spec_dir.glob("*.json"))
            json_files = [
                f for f in json_files
                if f.name != "LATEST.json"
                and not f.name.endswith(".tmp")
            ]

            if not json_files:
                continue

            # Fast-path: check last file for terminal target.  This avoids
            # parsing all files for specs that completed long ago.
            #
            # IMPORTANT (review finding C1): The last file is agent-written
            # and could be a rogue file (as in the 2026-04-11 incident where
            # a deployer wrote 018-docs with target:human).  To mitigate:
            #  - We validate chain continuity: if there are >=2 files, the
            #    second-to-last file's target must match the last file's
            #    source.  A rogue file inserted at the end breaks this chain.
            #  - We set handoff_count = len(json_files) so Phase 2 sequence
            #    validation has the correct baseline on future handoffs.
            try:
                with open(json_files[-1]) as fh:
                    last_data = json.load(fh)
                last_target = last_data.get("nextAgent", {}).get("target", "")
                last_source = last_data.get("metadata", {}).get("agent", "")
                if last_target in ("human", "none"):
                    # Chain validation: verify the last file is a legitimate
                    # successor to the second-to-last file.  If the chain
                    # breaks, fall through to full scan which will catch the
                    # inconsistency.
                    if len(json_files) >= 2:
                        try:
                            with open(json_files[-2]) as fh2:
                                prev_data = json.load(fh2)
                            prev_target = prev_data.get("nextAgent", {}).get("target", "")
                            if prev_target != last_source and last_source:
                                log.warning(
                                    f"Recovery: {spec_id} chain break — "
                                    f"penultimate targets {prev_target} but "
                                    f"last claims source={last_source}. "
                                    f"Falling through to full scan."
                                )
                                raise ValueError("chain break")
                        except (json.JSONDecodeError, OSError):
                            pass  # Can't read penultimate — trust last file
                    pipeline = self.pipeline.get_or_create_pipeline(spec_id)
                    pipeline.status = "completed"
                    pipeline.current_agent = None
                    pipeline.handoff_count = len(json_files)
                    total_specs += 1
                    continue
            except ValueError:
                pass  # Chain break — fall through to full scan
            except Exception:
                pass  # Parse error — fall through to full scan

            handoff_pairs: list[tuple[Handoff, Path]] = []
            for f in json_files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    handoff = Handoff(**data)
                    self.tracker.mark_processed(handoff, f)
                    handoff_pairs.append((handoff, f))
                except Exception:
                    pass

            handoffs = [h for h, _ in handoff_pairs]

            if handoffs:
                # Option B: Use daemon start time for staleness, not file mtime.
                # Any handoff from before this session started is stale.
                # We pass started_at as the threshold — files older than this
                # get no agent assignment. File mtime is unreliable (gets touched
                # by reconciliation, agents reading files, etc.)
                latest_file = json_files[-1]
                latest_mtime = latest_file.stat().st_mtime
                self.pipeline.recover_from_handoffs(
                    spec_id, handoffs,
                    latest_mtime=latest_mtime,
                    stale_threshold_seconds=0.0,  # not used; we override below
                )
                # Override: mark stale if file predates this daemon session
                pipeline = self.pipeline.get_pipeline(spec_id)
                if pipeline and latest_mtime < self._started_at:
                    if pipeline.status == "active":
                        # Release any agent assigned during recovery
                        for agent_name, sid in list(self.pipeline._agent_spec.items()):
                            if sid == spec_id:
                                self.pipeline.release_agent(agent_name)
                        pipeline.status = "stale"
                        pipeline.current_agent = None
                        log.info(
                            f"Recovery: {spec_id} marked stale "
                            f"(file predates session start by "
                            f"{self._started_at - latest_mtime:.0f}s)"
                        )
                total_specs += 1
                total_handoffs += len(handoffs)

                # Check if pipeline reached a terminal state
                latest_handoff, latest_path = handoff_pairs[-1]
                target = latest_handoff.target_agent

                if target in ("human", "none"):
                    # Pipeline is complete — mark it and skip unrouted check
                    self.pipeline.mark_completed(spec_id)
                    log.info(
                        f"Recovery: {spec_id} pipeline complete "
                        f"(target={target})"
                    )
                    continue

                # Phase 2.6: Detect unrouted handoffs — the latest handoff
                # for this spec may never have been dispatched to the target
                # agent (e.g., if IWO restarted after the file was written
                # but before routing occurred). Check if the target agent
                # has produced a subsequent handoff; if not, queue it.
                #
                # Note: we already checked if the LATEST handoff targets
                # human/none above (marks pipeline complete). For multi-sprint
                # specs, earlier sprints may have targeted human but the
                # current sprint is active — that's fine, only the latest
                # handoff matters for unrouted detection.

                target_responded = any(
                    h.source_agent == target
                    and h.sequence > latest_handoff.sequence
                    for h in handoffs
                )
                if not target_responded:
                    # Only queue if the handoff file is recent (last 24h).
                    # Old unrouted handoffs from abandoned specs should not
                    # be force-dispatched on every restart.
                    file_age_hours = (
                        time.time() - latest_path.stat().st_mtime
                    ) / 3600
                    if file_age_hours > 24:
                        log.info(
                            f"Recovery: {spec_id} has unrouted handoff "
                            f"#{latest_handoff.sequence} but file is "
                            f"{file_age_hours:.0f}h old — skipping"
                        )
                        continue

                    # Remove from tracker so process_handoff won't skip it
                    self.tracker._processed.discard(
                        latest_handoff.idempotency_key
                    )
                    self._pending_activations.append(
                        (latest_handoff, latest_path)
                    )
                    log.info(
                        f"Recovery: {spec_id} has UNROUTED handoff "
                        f"#{latest_handoff.sequence} "
                        f"{latest_handoff.source_agent}→{target} "
                        f"— queuing for activation"
                    )

        log.info(
            f"Recovery: {total_specs} spec(s), {total_handoffs} handoff(s) recovered"
        )
        if self._pending_activations:
            log.info(
                f"Recovery: {len(self._pending_activations)} unrouted "
                f"handoff(s) queued for activation"
            )

        # Post-recovery auto-continue: if all pipelines are complete/stale
        # and no unrouted handoffs need activation, fire a next-spec directive
        # so the pipeline resumes work from the queue automatically.
        if (
            self.config.auto_continue_on_completion
            and not self._pending_activations
            and self.pipeline.active_count == 0
            and total_specs > 0
        ):
            log.info(
                "Recovery: all pipelines complete — scheduling auto-continue"
            )
            self._schedule_auto_continue("recovery-restart")

        self._write_active_specs()


def main():
    """Entry point."""
    config = IWOConfig()
    if root := os.environ.get("IWO_PROJECT_ROOT"):
        config.project_root = Path(root)
        config.handoffs_dir = config.project_root / "docs" / "agent-comms"

    daemon = IWODaemon(config)
    daemon.start()


if __name__ == "__main__":
    main()
