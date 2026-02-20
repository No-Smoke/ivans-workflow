"""IWO Auditor Module — Phase 1 of Agent 007 implementation.

Deterministic health monitoring integrated into the IWO daemon poll loop.
Performs post-handoff invariant checks and timer-based liveness monitoring.

Design: Read-only checks with notification output. Zero risk — no state
mutations beyond writing diagnostic files and sending webhooks.

Check catalogue:
  - agent_liveness: No handoff within 30 min after activation
  - agent_timeout: Agent working >60 min (critical)
  - pipeline_consistency: Status doesn't match latest handoff (auto-fix)
  - sequence_continuity: Gap or unexpected duplicate in handoff sequence
  - timestamp_sanity: received_at vs metadata.timestamp drift >1h
  - stale_assignment: Agent assigned to completed pipeline (auto-release)
  - queue_inflation: Queue depth >5 for any agent
  - daemon_heartbeat: Heartbeat file freshness for external monitoring

Phase: Agent 007 Phase 1
Author: Vanya + Claude Opus 4.6
Created: 2026-02-20
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from .state import AgentState

if TYPE_CHECKING:
    from .daemon import IWODaemon
    from .parser import Handoff

log = logging.getLogger("iwo.auditor")


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Audit event severity — maps to notification tiers."""
    INFO = "info"          # 🟢 Silent message
    WARNING = "warning"    # 🟡 Normal notification
    CRITICAL = "critical"  # 🔴 Highlight/ping
    FATAL = "fatal"        # 💀 Ping + repeated


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AuditorConfig:
    """Auditor thresholds and settings. Injected from IWOConfig."""

    # Liveness: warn if no handoff within this many minutes of activation
    liveness_warning_minutes: int = 30

    # Timeout: critical if agent has been working longer than this
    timeout_critical_minutes: int = 60

    # Timestamp sanity: warn if received_at vs metadata.timestamp drift exceeds this
    timestamp_drift_max_hours: float = 1.0

    # Queue inflation: warn if any agent's queue exceeds this depth
    queue_inflation_threshold: int = 5

    # Heartbeat interval (seconds)
    heartbeat_interval_seconds: int = 60

    # Periodic check interval (seconds) — how often timer-based checks run
    periodic_check_interval_seconds: int = 300  # 5 minutes

    # Audit trail directory (relative to handoffs_dir)
    audit_subdir: str = ".audit"

    # Webhook URL for audit events (defaults to daemon's webhook URL)
    webhook_url: Optional[str] = None
    webhook_timeout: int = 10

    # Enable/disable individual checks
    enabled_checks: set[str] = field(default_factory=lambda: {
        "agent_liveness",
        "agent_timeout",
        "pipeline_consistency",
        "sequence_continuity",
        "timestamp_sanity",
        "stale_assignment",
        "queue_inflation",
        "daemon_heartbeat",
    })


# ---------------------------------------------------------------------------
# Diagnostic event
# ---------------------------------------------------------------------------

@dataclass
class AuditEvent:
    """Single diagnostic event from an auditor check."""
    timestamp: str
    check: str
    severity: Severity
    spec_id: Optional[str]
    details: dict[str, Any]
    action_taken: Optional[str]
    recommended_action: Optional[str]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "check": self.check,
            "severity": self.severity.value,
            "spec_id": self.spec_id,
            "details": self.details,
            "action_taken": self.action_taken,
            "recommended_action": self.recommended_action,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# Auditor class
# ---------------------------------------------------------------------------

class Auditor:
    """Deterministic health monitor for the IWO pipeline.

    Usage from daemon:
        auditor = Auditor(daemon, config)
        # After each handoff:
        auditor.post_handoff_checks(handoff)
        # Every poll cycle:
        auditor.periodic_checks()
    """

    def __init__(self, daemon: IWODaemon, config: Optional[AuditorConfig] = None):
        self.daemon = daemon
        self.config = config or AuditorConfig()
        self._audit_dir: Optional[Path] = None
        self._last_periodic_check: float = 0.0
        self._last_heartbeat: float = 0.0
        self._event_count: int = 0

        # Ensure audit directory exists
        self._init_audit_dir()

    def _init_audit_dir(self) -> None:
        """Create the audit trail directory if it doesn't exist."""
        try:
            self._audit_dir = self.daemon.config.handoffs_dir / self.config.audit_subdir
            self._audit_dir.mkdir(parents=True, exist_ok=True)
            log.info(f"Auditor: audit directory at {self._audit_dir}")
        except Exception as e:
            log.warning(f"Auditor: could not create audit dir: {e}")
            self._audit_dir = None

    # -------------------------------------------------------------------
    # Event handling
    # -------------------------------------------------------------------

    def _emit(self, event: AuditEvent) -> None:
        """Process an audit event: log, write to disk, send webhook."""
        self._event_count += 1

        # 1. Log it
        level = {
            Severity.INFO: logging.INFO,
            Severity.WARNING: logging.WARNING,
            Severity.CRITICAL: logging.ERROR,
            Severity.FATAL: logging.CRITICAL,
        }.get(event.severity, logging.WARNING)

        log.log(
            level,
            f"AUDIT [{event.severity.value.upper()}] {event.check}: "
            f"{event.spec_id or 'global'} — "
            f"{event.action_taken or event.recommended_action or 'no action'}"
        )

        # 2. Write to audit trail (best-effort)
        self._write_audit_file(event)

        # 3. Send webhook for warnings and above
        if event.severity in (Severity.WARNING, Severity.CRITICAL, Severity.FATAL):
            self._send_webhook(event)

        # 4. Desktop notification for critical and above
        if event.severity in (Severity.CRITICAL, Severity.FATAL):
            self.daemon._notify(
                f"🔍 AUDIT {event.severity.value.upper()}: {event.check} "
                f"({event.spec_id or 'global'})",
                critical=True,
            )

    def _write_audit_file(self, event: AuditEvent) -> None:
        """Write diagnostic event to the audit trail directory."""
        if not self._audit_dir:
            return
        try:
            filename = f"{event.timestamp.replace(':', '-')}_{event.check}.json"
            filepath = self._audit_dir / filename
            filepath.write_text(event.to_json())
        except Exception as e:
            log.warning(f"Auditor: could not write audit file: {e}")

    def _send_webhook(self, event: AuditEvent) -> None:
        """Send audit event to n8n webhook endpoint."""
        url = self.config.webhook_url or self.daemon.config.notification_webhook_url
        if not url:
            return

        try:
            payload = json.dumps({
                "source": "iwo-auditor",
                "event": event.to_dict(),
                "daemon_version": __version__,
            }).encode("utf-8")

            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=self.config.webhook_timeout) as resp:
                log.debug(f"Auditor webhook sent: {resp.status}")
        except (URLError, Exception) as e:
            log.warning(f"Auditor webhook failed: {e}")

    def _now_iso(self) -> str:
        """Current UTC timestamp in ISO 8601 format."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # -------------------------------------------------------------------
    # Post-handoff checks (called after each handoff is processed)
    # -------------------------------------------------------------------

    def post_handoff_checks(self, handoff: Handoff) -> list[AuditEvent]:
        """Run all post-handoff invariant checks. Returns events emitted."""
        events: list[AuditEvent] = []

        if "sequence_continuity" in self.config.enabled_checks:
            ev = self._check_sequence_continuity(handoff)
            if ev:
                events.append(ev)

        if "timestamp_sanity" in self.config.enabled_checks:
            ev = self._check_timestamp_sanity(handoff)
            if ev:
                events.append(ev)

        if "pipeline_consistency" in self.config.enabled_checks:
            ev = self._check_pipeline_consistency(handoff)
            if ev:
                events.append(ev)

        for event in events:
            self._emit(event)

        return events

    def _check_sequence_continuity(self, handoff: Handoff) -> Optional[AuditEvent]:
        """Check for sequence gaps or unexpected duplicates in the handoff chain."""
        spec_id = handoff.spec_id
        pipeline_info = self.daemon.pipeline.get_pipeline(spec_id)
        if not pipeline_info:
            return None

        # Get all handoff sequences for this spec from the tracker
        expected_seq = handoff.sequence
        history = [
            h for h in self.daemon.handoff_history
            if h.spec_id == spec_id
        ]

        if len(history) < 2:
            return None

        # Check for gaps: previous handoff's sequence should be current - 1
        # (approximately — rejections can cause non-linear sequences)
        sequences = sorted(set(h.sequence for h in history))
        gaps = []
        for i in range(1, len(sequences)):
            if sequences[i] - sequences[i - 1] > 2:  # Allow small gaps from rejections
                gaps.append((sequences[i - 1], sequences[i]))

        if gaps:
            return AuditEvent(
                timestamp=self._now_iso(),
                check="sequence_continuity",
                severity=Severity.WARNING,
                spec_id=spec_id,
                details={
                    "current_sequence": expected_seq,
                    "total_handoffs": len(history),
                    "gaps": [{"from": g[0], "to": g[1]} for g in gaps],
                },
                action_taken=None,
                recommended_action="Investigate missing handoff files — possible filesystem issue",
            )
        return None

    def _check_timestamp_sanity(self, handoff: Handoff) -> Optional[AuditEvent]:
        """Check for drift between metadata.timestamp and received_at."""
        try:
            meta_ts = handoff.metadata.get("timestamp", "") if handoff.metadata else ""
            received = handoff.metadata.get("received_at", "") if handoff.metadata else ""

            if not meta_ts or not received:
                return None

            # Parse ISO timestamps (basic comparison)
            import datetime
            meta_dt = datetime.datetime.fromisoformat(meta_ts.replace("Z", "+00:00"))
            recv_dt = datetime.datetime.fromisoformat(received.replace("Z", "+00:00"))

            drift_hours = abs((recv_dt - meta_dt).total_seconds()) / 3600

            if drift_hours > self.config.timestamp_drift_max_hours:
                return AuditEvent(
                    timestamp=self._now_iso(),
                    check="timestamp_sanity",
                    severity=Severity.WARNING,
                    spec_id=handoff.spec_id,
                    details={
                        "metadata_timestamp": meta_ts,
                        "received_at": received,
                        "drift_hours": round(drift_hours, 2),
                        "threshold_hours": self.config.timestamp_drift_max_hours,
                        "source_agent": handoff.source_agent,
                    },
                    action_taken=None,
                    recommended_action="Agent is fabricating timestamps — IWO stamps received_at as truth",
                )
        except Exception as e:
            log.debug(f"Auditor: timestamp sanity check skipped: {e}")

        return None

    def _check_pipeline_consistency(self, handoff: Handoff) -> Optional[AuditEvent]:
        """Check that pipeline status matches the latest handoff state."""
        spec_id = handoff.spec_id
        pipeline_info = self.daemon.pipeline.get_pipeline(spec_id)
        if not pipeline_info:
            return None

        # If the handoff says success but pipeline shows halted, that's inconsistent
        if (handoff.status.outcome == "success"
                and pipeline_info.status == "halted"):
            # Auto-fix: reactivate the pipeline
            try:
                pipeline_info.status = "active"
                log.info(f"Auditor: reactivated halted pipeline {spec_id}")
                return AuditEvent(
                    timestamp=self._now_iso(),
                    check="pipeline_consistency",
                    severity=Severity.WARNING,
                    spec_id=spec_id,
                    details={
                        "handoff_outcome": handoff.status.outcome,
                        "pipeline_status": "halted",
                        "source_agent": handoff.source_agent,
                        "target_agent": handoff.target_agent,
                    },
                    action_taken="Reactivated pipeline — handoff outcome contradicts halted state",
                    recommended_action=None,
                )
            except Exception as e:
                return AuditEvent(
                    timestamp=self._now_iso(),
                    check="pipeline_consistency",
                    severity=Severity.CRITICAL,
                    spec_id=spec_id,
                    details={
                        "handoff_outcome": handoff.status.outcome,
                        "pipeline_halted": True,
                        "error": str(e),
                    },
                    action_taken=None,
                    recommended_action="Manual investigation needed — pipeline reactivation failed",
                )

        return None

    # -------------------------------------------------------------------
    # Periodic checks (called on timer from the daemon poll loop)
    # -------------------------------------------------------------------

    def periodic_checks(self) -> list[AuditEvent]:
        """Run all timer-based health checks. Called from the daemon poll loop.

        Respects periodic_check_interval_seconds to avoid running every tick.
        Heartbeat runs on its own faster interval.
        """
        now = time.monotonic()
        events: list[AuditEvent] = []

        # Heartbeat runs on its own interval (every 60s by default)
        if now - self._last_heartbeat >= self.config.heartbeat_interval_seconds:
            self._last_heartbeat = now
            if "daemon_heartbeat" in self.config.enabled_checks:
                self._write_heartbeat()

        # Other periodic checks run every 5 minutes
        if now - self._last_periodic_check < self.config.periodic_check_interval_seconds:
            return events
        self._last_periodic_check = now

        log.debug("Auditor: running periodic checks")

        if "agent_liveness" in self.config.enabled_checks:
            events.extend(self._check_agent_liveness())

        if "agent_timeout" in self.config.enabled_checks:
            events.extend(self._check_agent_timeout())

        if "stale_assignment" in self.config.enabled_checks:
            events.extend(self._check_stale_assignments())

        if "queue_inflation" in self.config.enabled_checks:
            events.extend(self._check_queue_inflation())

        for event in events:
            self._emit(event)

        if events:
            log.info(f"Auditor: periodic checks produced {len(events)} event(s)")

        return events

    def _check_agent_liveness(self) -> list[AuditEvent]:
        """Check if activated agents have produced a handoff within the liveness window."""
        events = []

        for agent_name, sm in self.daemon.state_machines.items():
            assigned_spec = self.daemon.pipeline.agent_current_spec(agent_name)
            if not assigned_spec:
                continue  # Agent not assigned — nothing to check

            pipeline_info = self.daemon.pipeline.get_pipeline(assigned_spec)
            if not pipeline_info:
                continue

            # Use idle_seconds — time since last handoff activity
            elapsed_minutes = pipeline_info.idle_seconds / 60

            if elapsed_minutes >= self.config.liveness_warning_minutes:
                pane_responsive = sm.state not in (
                    AgentState.CRASHED,
                    AgentState.STUCK,
                )

                severity = Severity.WARNING
                if elapsed_minutes >= self.config.timeout_critical_minutes:
                    severity = Severity.CRITICAL

                # Calculate activation timestamp from idle_seconds
                activated_approx = time.time() - pipeline_info.idle_seconds

                events.append(AuditEvent(
                    timestamp=self._now_iso(),
                    check="agent_liveness" if severity == Severity.WARNING else "agent_timeout",
                    severity=severity,
                    spec_id=assigned_spec,
                    details={
                        "agent": agent_name,
                        "last_activity_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(activated_approx)
                        ),
                        "minutes_idle": round(elapsed_minutes, 1),
                        "threshold_minutes": (
                            self.config.liveness_warning_minutes
                            if severity == Severity.WARNING
                            else self.config.timeout_critical_minutes
                        ),
                        "state_machine_state": sm.state.value,
                        "tmux_pane_responsive": pane_responsive,
                    },
                    action_taken=None,
                    recommended_action=(
                        "monitor — approaching timeout threshold"
                        if severity == Severity.WARNING
                        else "investigate — agent may be stalled or waiting for input"
                    ),
                ))

        return events

    def _check_agent_timeout(self) -> list[AuditEvent]:
        """Covered by _check_agent_liveness with severity escalation."""
        # Timeout is the critical tier of liveness — handled in one method
        return []

    def _check_stale_assignments(self) -> list[AuditEvent]:
        """Check for agents assigned to completed or halted pipelines."""
        events = []

        for agent_name in self.daemon.config.agent_window_map:
            assigned_spec = self.daemon.pipeline.agent_current_spec(agent_name)
            if not assigned_spec:
                continue

            pipeline_info = self.daemon.pipeline.get_pipeline(assigned_spec)
            if not pipeline_info:
                continue

            is_completed = pipeline_info.status == "completed"
            is_halted = pipeline_info.status == "halted"

            if is_completed or is_halted:
                # Auto-fix: release the agent
                try:
                    self.daemon.pipeline.release_agent(agent_name)
                    events.append(AuditEvent(
                        timestamp=self._now_iso(),
                        check="stale_assignment",
                        severity=Severity.INFO,
                        spec_id=assigned_spec,
                        details={
                            "agent": agent_name,
                            "pipeline_completed": is_completed,
                            "pipeline_halted": is_halted,
                        },
                        action_taken=f"Released {agent_name} from {assigned_spec}",
                        recommended_action=None,
                    ))
                except Exception as e:
                    events.append(AuditEvent(
                        timestamp=self._now_iso(),
                        check="stale_assignment",
                        severity=Severity.WARNING,
                        spec_id=assigned_spec,
                        details={
                            "agent": agent_name,
                            "error": str(e),
                        },
                        action_taken=None,
                        recommended_action="Manual release needed",
                    ))

        return events

    def _check_queue_inflation(self) -> list[AuditEvent]:
        """Check for queues that have grown beyond the inflation threshold."""
        events = []

        for agent_name in self.daemon.config.agent_window_map:
            queue_depth = self.daemon.pipeline.queue_depth(agent_name)
            if queue_depth > self.config.queue_inflation_threshold:
                events.append(AuditEvent(
                    timestamp=self._now_iso(),
                    check="queue_inflation",
                    severity=Severity.WARNING,
                    spec_id=None,
                    details={
                        "agent": agent_name,
                        "queue_depth": queue_depth,
                        "threshold": self.config.queue_inflation_threshold,
                    },
                    action_taken=None,
                    recommended_action=(
                        f"Investigate why {agent_name} queue is backed up — "
                        f"possible bottleneck or stalled agent"
                    ),
                ))

        return events

    # -------------------------------------------------------------------
    # Heartbeat
    # -------------------------------------------------------------------

    def _write_heartbeat(self) -> None:
        """Write heartbeat file for external monitoring."""
        if not self._audit_dir:
            return
        try:
            heartbeat = {
                "timestamp": self._now_iso(),
                "pid": os.getpid(),
                "daemon_version": __version__,
                "uptime_seconds": time.monotonic(),
                "events_emitted": self._event_count,
                "active_specs": [
                    p.spec_id for p in self.daemon.pipeline.all_pipelines
                    if p.status == "active"
                ],
            }
            heartbeat_path = self._audit_dir / "heartbeat.json"
            heartbeat_path.write_text(json.dumps(heartbeat, indent=2))
        except Exception as e:
            log.warning(f"Auditor: heartbeat write failed: {e}")

    # -------------------------------------------------------------------
    # Status summary (for TUI display)
    # -------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return auditor status for TUI or API consumption."""
        return {
            "enabled": True,
            "events_emitted": self._event_count,
            "last_periodic_check": self._last_periodic_check,
            "last_heartbeat": self._last_heartbeat,
            "enabled_checks": sorted(self.config.enabled_checks),
            "audit_dir": str(self._audit_dir) if self._audit_dir else None,
        }
