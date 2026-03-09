"""Multi-spec pipeline tracking — Phase 2.3.1.

Tracks multiple concurrent specifications progressing through the
Boris workflow pipeline. Each spec has its own lifecycle (active →
completed | halted) and tracks which agent is currently working on it.

Queued handoffs respect rejection-first priority: incomplete work
(rejections back to a previous agent) always takes precedence over
new spec work arriving for the same agent.

Design: Claude Opus 4.6 interactive session, 2026-02-19.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

from .parser import Handoff

log = logging.getLogger("iwo.pipeline")


@dataclass
class SpecPipeline:
    """Lifecycle tracker for a single specification."""

    spec_id: str
    current_agent: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    status: str = "active"  # active | completed | halted | queued
    handoff_count: int = 0
    last_handoff_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.started_at

    @property
    def idle_seconds(self) -> float:
        """Seconds since last handoff activity."""
        ref = self.last_handoff_at if self.last_handoff_at else self.started_at
        return time.time() - ref


@dataclass
class QueuedHandoff:
    """A handoff waiting for its target agent to become available."""

    handoff: Handoff
    path: Path
    queued_at: float = field(default_factory=time.time)

    @property
    def is_rejection(self) -> bool:
        return self.handoff.is_rejection

    @property
    def target_agent(self) -> str:
        return self.handoff.target_agent

    @property
    def spec_id(self) -> str:
        return self.handoff.spec_id


class PipelineManager:
    """Manages multiple concurrent spec pipelines and agent-handoff queuing.

    Core invariant: each agent can only work on ONE spec at a time.
    When a handoff arrives for a busy agent, it's queued. When an agent
    finishes (detected by seeing a handoff FROM that agent), the queue
    is drained with rejection-first priority.
    """

    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent

        # Spec tracking
        self._pipelines: dict[str, SpecPipeline] = {}

        # Agent → spec assignment (reverse lookup)
        self._agent_spec: dict[str, Optional[str]] = {}

        # Per-agent queue of waiting handoffs (ordered: rejections first, then FIFO)
        self._agent_queue: dict[str, list[QueuedHandoff]] = {}

    # ── Pipeline CRUD ────────────────────────────────────────────────

    def get_or_create_pipeline(self, spec_id: str) -> SpecPipeline:
        """Get existing pipeline or create a new one."""
        if spec_id not in self._pipelines:
            if self.active_count >= self.max_concurrent:
                log.warning(
                    f"Max concurrent specs ({self.max_concurrent}) reached. "
                    f"Spec {spec_id} will be tracked but may queue."
                )
            self._pipelines[spec_id] = SpecPipeline(spec_id=spec_id)
            log.info(f"Pipeline created: {spec_id}")
        return self._pipelines[spec_id]

    def get_pipeline(self, spec_id: str) -> Optional[SpecPipeline]:
        return self._pipelines.get(spec_id)

    @property
    def active_count(self) -> int:
        return sum(1 for p in self._pipelines.values() if p.status == "active")

    @property
    def all_pipelines(self) -> list[SpecPipeline]:
        """All pipelines sorted: active first, then by recency."""
        return sorted(
            self._pipelines.values(),
            key=lambda p: (p.status != "active", -p.last_handoff_at),
        )

    # ── Agent assignment ─────────────────────────────────────────────

    def assign_agent(self, agent: str, spec_id: str):
        """Record that an agent is now working on a spec."""
        prev = self._agent_spec.get(agent)
        if prev and prev != spec_id:
            log.info(f"Agent {agent}: reassigned {prev} → {spec_id}")
        self._agent_spec[agent] = spec_id

        pipeline = self.get_pipeline(spec_id)
        if pipeline:
            pipeline.current_agent = agent

    def release_agent(self, agent: str) -> Optional[str]:
        """Mark agent as no longer working on any spec. Returns the released spec_id."""
        spec_id = self._agent_spec.pop(agent, None)
        if spec_id:
            pipeline = self.get_pipeline(spec_id)
            if pipeline and pipeline.current_agent == agent:
                pipeline.current_agent = None
            log.info(f"Agent {agent}: released from {spec_id}")
        return spec_id

    def agent_current_spec(self, agent: str) -> Optional[str]:
        """Which spec is this agent currently working on?"""
        return self._agent_spec.get(agent)

    def is_agent_busy(self, agent: str) -> bool:
        return agent in self._agent_spec and self._agent_spec[agent] is not None

    # ── Staleness cleanup (Bug 3 fix) ───────────────────────────────

    def release_stale_pipelines(self, stale_threshold_seconds: float) -> list[str]:
        """Release agent assignments from pipelines with no recent activity.

        Returns list of spec_ids that were marked stale.
        Called periodically from daemon poll loop.
        """
        released = []
        for spec_id, pipeline in list(self._pipelines.items()):
            if pipeline.status != "active":
                continue
            if pipeline.idle_seconds > stale_threshold_seconds:
                # Release any agent assigned to this stale pipeline
                for agent, sid in list(self._agent_spec.items()):
                    if sid == spec_id:
                        self.release_agent(agent)
                pipeline.status = "stale"
                released.append(spec_id)
                log.info(
                    f"Pipeline stale: {spec_id} "
                    f"(no activity for {pipeline.idle_seconds:.0f}s, "
                    f"threshold {stale_threshold_seconds:.0f}s)"
                )
        return released

    # ── Handoff queue ────────────────────────────────────────────────

    def enqueue(self, handoff: Handoff, path: Path):
        """Add a handoff to the target agent's queue."""
        target = handoff.target_agent
        if target not in self._agent_queue:
            self._agent_queue[target] = []

        queued = QueuedHandoff(handoff=handoff, path=path)
        self._agent_queue[target].append(queued)
        self._sort_queue(target)

        log.info(
            f"Queued: {handoff.spec_id} #{handoff.sequence} → {target} "
            f"(queue depth: {len(self._agent_queue[target])}, "
            f"rejection: {handoff.is_rejection})"
        )

    def dequeue(self, agent: str) -> Optional[QueuedHandoff]:
        """Pop the highest-priority handoff for this agent.

        Priority: rejections first (incomplete work), then FIFO.
        Returns None if queue is empty.
        """
        queue = self._agent_queue.get(agent, [])
        if not queue:
            return None
        return queue.pop(0)

    def peek_queue(self, agent: str) -> Optional[QueuedHandoff]:
        """Look at next queued item without removing it."""
        queue = self._agent_queue.get(agent, [])
        return queue[0] if queue else None

    def queue_depth(self, agent: str) -> int:
        return len(self._agent_queue.get(agent, []))

    def total_queued(self) -> int:
        return sum(len(q) for q in self._agent_queue.values())

    def _sort_queue(self, agent: str):
        """Sort queue: rejections first, then by queue time (FIFO)."""
        queue = self._agent_queue.get(agent, [])
        queue.sort(key=lambda qh: (not qh.is_rejection, qh.queued_at))

    # ── Handoff processing helpers ───────────────────────────────────

    def record_handoff(self, handoff: Handoff):
        """Update pipeline state when a handoff is processed.

        Called after validation/idempotency checks pass.
        Updates the source agent's pipeline and increments counters.
        Reactivates completed pipelines for multi-sprint specs.
        """
        pipeline = self.get_or_create_pipeline(handoff.spec_id)
        pipeline.handoff_count += 1
        pipeline.last_handoff_at = time.time()

        # Reactivate if a new sprint starts on a completed pipeline
        if pipeline.status == "completed":
            pipeline.status = "active"
            log.info(
                f"Pipeline reactivated: {handoff.spec_id} "
                f"(new handoff from {handoff.source_agent})"
            )

        # Source agent is done with their stage of this spec
        source = handoff.source_agent
        if self._agent_spec.get(source) == handoff.spec_id:
            self.release_agent(source)

    def mark_completed(self, spec_id: str):
        """Mark a spec pipeline as completed (e.g., after docs agent finishes)."""
        pipeline = self.get_pipeline(spec_id)
        if pipeline:
            pipeline.status = "completed"
            # Release any agent still assigned
            for agent, sid in list(self._agent_spec.items()):
                if sid == spec_id:
                    self.release_agent(agent)
            log.info(f"Pipeline completed: {spec_id}")

    def mark_halted(self, spec_id: str, reason: str = ""):
        """Mark a spec pipeline as halted (safety rail triggered)."""
        pipeline = self.get_pipeline(spec_id)
        if pipeline:
            pipeline.status = "halted"
            log.warning(f"Pipeline halted: {spec_id} — {reason}")

    # ── State persistence ────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize pipeline state for .active-specs.json."""
        return {
            "pipelines": {
                sid: {
                    "current_agent": p.current_agent,
                    "status": p.status,
                    "handoff_count": p.handoff_count,
                    "started_at": p.started_at,
                    "last_handoff_at": p.last_handoff_at,
                }
                for sid, p in self._pipelines.items()
            },
            "agent_assignments": {
                agent: spec
                for agent, spec in self._agent_spec.items()
                if spec is not None
            },
            "queue_depths": {
                agent: len(queue)
                for agent, queue in self._agent_queue.items()
                if queue
            },
            "timestamp": time.time(),
        }

    # ── Recovery ─────────────────────────────────────────────────────

    def recover_from_handoffs(self, spec_id: str, handoffs: list[Handoff],
                               latest_mtime: float = 0.0,
                               stale_threshold_seconds: float = 14400.0):
        """Reconstruct pipeline state from a list of existing handoffs.

        Called during daemon startup to rebuild state from filesystem.
        Handoffs should be in chronological order (by sequence number).

        If latest_mtime is provided and older than stale_threshold_seconds,
        the pipeline is marked stale and no agent assignment is made (Bug 3 fix).
        """
        if not handoffs:
            return

        pipeline = self.get_or_create_pipeline(spec_id)
        pipeline.handoff_count = len(handoffs)

        latest = handoffs[-1]
        pipeline.last_handoff_at = latest_mtime if latest_mtime else pipeline.started_at

        # Check staleness — don't assign agents to old pipelines
        if latest_mtime and (time.time() - latest_mtime > stale_threshold_seconds):
            pipeline.status = "stale"
            pipeline.current_agent = None
            log.info(
                f"Recovered pipeline {spec_id} as STALE: "
                f"{pipeline.handoff_count} handoffs, "
                f"last activity {time.time() - latest_mtime:.0f}s ago"
            )
            return

        # The latest handoff tells us who should be working now
        target = latest.target_agent
        if target in ("human", "none"):
            # Terminal target — no agent assignment needed
            pipeline.current_agent = target
        elif latest.status.outcome != "failed":
            pipeline.current_agent = target
            self._agent_spec[target] = spec_id
        else:
            # Rejection: the rejection target should be working
            pipeline.current_agent = target
            self._agent_spec[target] = spec_id

        log.info(
            f"Recovered pipeline {spec_id}: "
            f"{pipeline.handoff_count} handoffs, "
            f"current_agent={pipeline.current_agent}"
        )
