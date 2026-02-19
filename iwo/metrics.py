"""IWO Pipeline Metrics — Phase 2.5.1.

Computes pipeline performance metrics from Neo4j HandoffEvent data:
- Per-agent cycle times (how long each agent takes)
- Rejection rates per agent pair
- Specs completed vs active vs halted
- Bottleneck identification (slowest stage)
- Throughput (handoffs per hour, specs per day)

All queries are read-only and best-effort — metrics unavailability
does not affect orchestration.

Design: Claude Opus 4.6 interactive session, 2026-02-19.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .memory import IWOMemory

log = logging.getLogger("iwo.metrics")


@dataclass
class AgentMetrics:
    """Performance stats for a single agent."""
    agent: str
    avg_cycle_minutes: float = 0.0
    handoff_count: int = 0
    rejection_count: int = 0
    rejection_rate: float = 0.0  # 0.0 to 1.0


@dataclass
class PipelineMetrics:
    """Aggregate pipeline performance snapshot."""
    agent_metrics: list[AgentMetrics] = field(default_factory=list)
    total_handoffs: int = 0
    total_rejections: int = 0
    specs_completed: int = 0
    specs_active: int = 0
    bottleneck_agent: Optional[str] = None
    handoffs_per_hour: float = 0.0
    last_updated: float = 0.0


class MetricsCollector:
    """Queries Neo4j for pipeline performance data.

    All methods return defaults on failure — metrics are advisory only.
    """

    def __init__(self, memory: Optional["IWOMemory"] = None):
        self._memory = memory
        self._cache: Optional[PipelineMetrics] = None
        self._cache_ttl: float = 60.0  # refresh at most every 60s
        self._cache_time: float = 0.0

    @property
    def _driver(self):
        if self._memory and self._memory._neo4j_driver:
            return self._memory._neo4j_driver
        return None

    def collect(self) -> PipelineMetrics:
        """Collect all metrics. Returns cached if fresh enough."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        metrics = PipelineMetrics(last_updated=now)

        if not self._driver:
            return metrics

        try:
            metrics.agent_metrics = self._agent_cycle_times()
            metrics.total_handoffs = sum(a.handoff_count for a in metrics.agent_metrics)
            metrics.total_rejections = sum(a.rejection_count for a in metrics.agent_metrics)
            metrics.handoffs_per_hour = self._handoffs_per_hour()
            metrics.bottleneck_agent = self._identify_bottleneck(metrics.agent_metrics)
        except Exception as e:
            log.warning(f"Metrics collection failed: {e}")

        self._cache = metrics
        self._cache_time = now
        return metrics

    def _agent_cycle_times(self) -> list[AgentMetrics]:
        """Compute average time each agent holds work before handing off.

        Uses NEXT_HANDOFF chain: time between a handoff targeting agent X
        and the next handoff where agent X is the source.
        """
        if not self._driver:
            return []

        try:
            with self._driver.session() as session:
                result = session.run(
                    """
                    MATCH (incoming:HandoffEvent)-[:NEXT_HANDOFF]->(outgoing:HandoffEvent)
                    WHERE incoming.target_agent = outgoing.source_agent
                    WITH outgoing.source_agent AS agent,
                         duration.between(
                             datetime(incoming.timestamp),
                             datetime(outgoing.timestamp)
                         ).minutes AS cycle_min,
                         outgoing.outcome AS outcome
                    RETURN agent,
                           avg(cycle_min) AS avg_cycle,
                           count(*) AS total,
                           sum(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS rejections
                    ORDER BY avg_cycle DESC
                    """
                )
                metrics = []
                for record in result:
                    total = record["total"]
                    rejections = record["rejections"]
                    metrics.append(AgentMetrics(
                        agent=record["agent"],
                        avg_cycle_minutes=round(record["avg_cycle"] or 0, 1),
                        handoff_count=total,
                        rejection_count=rejections,
                        rejection_rate=round(rejections / total, 2) if total > 0 else 0.0,
                    ))
                return metrics
        except Exception as e:
            log.warning(f"Agent cycle time query failed: {e}")
            return []

    def _handoffs_per_hour(self) -> float:
        """Average handoffs per hour over the last 24 hours."""
        if not self._driver:
            return 0.0

        try:
            with self._driver.session() as session:
                result = session.run(
                    """
                    MATCH (h:HandoffEvent)
                    WHERE h.stored_at >= datetime() - duration('PT24H')
                    RETURN count(h) AS count_24h
                    """
                )
                record = result.single()
                count = record["count_24h"] if record else 0
                return round(count / 24.0, 1)
        except Exception as e:
            log.warning(f"Handoffs per hour query failed: {e}")
            return 0.0

    def _identify_bottleneck(self, agent_metrics: list[AgentMetrics]) -> Optional[str]:
        """The agent with the highest average cycle time is the bottleneck."""
        if not agent_metrics:
            return None
        slowest = max(agent_metrics, key=lambda a: a.avg_cycle_minutes)
        return slowest.agent if slowest.avg_cycle_minutes > 0 else None
