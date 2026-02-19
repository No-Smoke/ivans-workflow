"""IWO Memory Integration — Pipeline History Storage.

Stores handoff events to Qdrant (semantic search) and Neo4j (relationship graph).
Uses Ollama mxbai-embed-large for 1024-dim embeddings, matching tos-bridge.

All operations are best-effort: if memory services are unavailable,
IWO continues orchestrating without interruption.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import IWOConfig
    from .parser import Handoff

log = logging.getLogger("iwo.memory")

# Collection name for pipeline history
COLLECTION = "iwo_pipeline_history"
VECTOR_DIM = 1024


class IWOMemory:
    """Non-blocking memory storage for pipeline telemetry."""

    def __init__(self, config: "IWOConfig"):
        self.config = config
        self._qdrant = None
        self._neo4j_driver = None
        self._available = False
        self._init_attempted = False

    def initialize(self) -> bool:
        """Connect to Qdrant and Neo4j. Returns True if both connected."""
        if self._init_attempted:
            return self._available
        self._init_attempted = True

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance, VectorParams, PointStruct,
            )
            self._qdrant = QdrantClient(
                url=self.config.qdrant_url,
                api_key=self.config.qdrant_api_key or None,
                timeout=10,
            )
            # Ensure collection exists
            collections = [c.name for c in self._qdrant.get_collections().collections]
            if COLLECTION not in collections:
                self._qdrant.create_collection(
                    collection_name=COLLECTION,
                    vectors_config=VectorParams(
                        size=VECTOR_DIM,
                        distance=Distance.COSINE,
                    ),
                )
                log.info(f"Created Qdrant collection: {COLLECTION}")
            else:
                log.info(f"Qdrant collection exists: {COLLECTION}")
        except Exception as e:
            log.warning(f"Qdrant init failed (memory disabled): {e}")
            self._qdrant = None

        try:
            from neo4j import GraphDatabase
            self._neo4j_driver = GraphDatabase.driver(
                self.config.neo4j_uri,
                auth=(self.config.neo4j_user, self.config.neo4j_password),
            )
            # Verify connectivity
            self._neo4j_driver.verify_connectivity()
            log.info("Neo4j connected")
        except Exception as e:
            log.warning(f"Neo4j init failed (graph disabled): {e}")
            self._neo4j_driver = None

        self._available = self._qdrant is not None or self._neo4j_driver is not None
        if self._available:
            log.info("Memory integration active")
        else:
            log.warning("Memory integration unavailable — running without persistence")
        return self._available

    def health_check(self) -> dict[str, bool]:
        """Check connectivity of all memory backends.

        Returns dict with keys 'qdrant', 'neo4j', 'ollama' mapping to bool.
        Each check has a short timeout to avoid blocking the TUI.
        """
        health = {"qdrant": False, "neo4j": False, "ollama": False}

        # Qdrant: list collections as a ping
        if self._qdrant:
            try:
                self._qdrant.get_collections()
                health["qdrant"] = True
            except Exception:
                pass

        # Neo4j: verify connectivity
        if self._neo4j_driver:
            try:
                self._neo4j_driver.verify_connectivity()
                health["neo4j"] = True
            except Exception:
                pass

        # Ollama: check embedding model availability
        try:
            import urllib.request
            url = f"{self.config.ollama_url}/api/tags"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    health["ollama"] = True
        except Exception:
            pass

        return health

    def store_handoff(self, handoff: "Handoff", processing_time_ms: float = 0):
        """Store a processed handoff to Qdrant and Neo4j.

        Best-effort: failures are logged but don't interrupt IWO.
        """
        if not self._available:
            return

        summary = self._build_summary(handoff)
        metadata = self._build_metadata(handoff, processing_time_ms)

        # Qdrant: semantic search over pipeline history
        if self._qdrant:
            try:
                self._store_to_qdrant(summary, metadata)
            except Exception as e:
                log.warning(f"Qdrant store failed: {e}")

        # Neo4j: relationship graph
        if self._neo4j_driver:
            try:
                self._store_to_neo4j(handoff, metadata, summary)
            except Exception as e:
                log.warning(f"Neo4j store failed: {e}")

    def query_spec_history(self, spec_id: str) -> list[dict]:
        """Get all handoff events for a spec from Neo4j."""
        if not self._neo4j_driver:
            return []
        try:
            with self._neo4j_driver.session() as session:
                result = session.run(
                    """
                    MATCH (h:HandoffEvent {spec_id: $spec_id})
                    RETURN h ORDER BY h.sequence ASC
                    """,
                    spec_id=spec_id,
                )
                return [dict(record["h"]) for record in result]
        except Exception as e:
            log.warning(f"Neo4j query failed: {e}")
            return []

    def get_pipeline_stats(self, spec_id: str) -> dict:
        """Aggregate pipeline stats for a spec."""
        if not self._neo4j_driver:
            return {}
        try:
            with self._neo4j_driver.session() as session:
                result = session.run(
                    """
                    MATCH (h:HandoffEvent {spec_id: $spec_id})
                    RETURN
                        count(h) as total_handoffs,
                        sum(CASE WHEN h.outcome = 'failed' THEN 1 ELSE 0 END) as rejections,
                        collect(h.source_agent + ' → ' + h.target_agent) as handoff_chain,
                        min(h.timestamp) as started,
                        max(h.timestamp) as last_handoff
                    """,
                    spec_id=spec_id,
                )
                record = result.single()
                if record:
                    return dict(record)
                return {}
        except Exception as e:
            log.warning(f"Neo4j stats query failed: {e}")
            return {}

    def search_similar_handoffs(self, query: str, limit: int = 5) -> list[dict]:
        """Semantic search over pipeline history."""
        if not self._qdrant:
            return []
        try:
            vector = self._embed(query)
            if not vector:
                return []
            from qdrant_client.models import Filter
            results = self._qdrant.search(
                collection_name=COLLECTION,
                query_vector=vector,
                limit=limit,
            )
            return [
                {"score": r.score, "summary": r.payload.get("summary", ""), **r.payload}
                for r in results
            ]
        except Exception as e:
            log.warning(f"Qdrant search failed: {e}")
            return []

    def close(self):
        """Clean up connections."""
        if self._neo4j_driver:
            try:
                self._neo4j_driver.close()
            except Exception:
                pass
        # qdrant-client doesn't need explicit close

    # --- Private helpers ---

    def _build_summary(self, handoff: "Handoff") -> str:
        """Build a searchable text summary from a handoff."""
        parts = [
            f"Spec {handoff.spec_id}: {handoff.source_agent} → {handoff.target_agent}.",
            f"Outcome: {handoff.status.outcome}.",
        ]
        if handoff.nextAgent:
            parts.append(f"Action: {handoff.nextAgent.action}.")
            if handoff.nextAgent.context:
                parts.append(f"Context: {handoff.nextAgent.context[:200]}.")
        if handoff.deliverables:
            d = handoff.deliverables
            all_files = handoff.files_touched
            if all_files:
                parts.append(f"Files ({len(all_files)}): {', '.join(all_files[:8])}.")
            if d.testsStatus:
                parts.append(f"Tests: {handoff.test_summary}.")
            if d.typecheckPassed is not None:
                parts.append(f"Typecheck: {'passed' if d.typecheckPassed else 'FAILED'}.")
        if handoff.evidence:
            if handoff.evidence.securityCheck:
                parts.append(f"Security: {handoff.evidence.securityCheck[:100]}.")
        if handoff.status.reviewFindings:
            rf = handoff.status.reviewFindings
            parts.append(f"Review: {len(rf.blocking)} blocking, {len(rf.medium)} medium, {len(rf.low)} low.")
        if handoff.status.deviationsFromPlan:
            parts.append(f"Deviations: {len(handoff.status.deviationsFromPlan)}.")
        return " ".join(parts)

    def _build_metadata(self, handoff: "Handoff", processing_time_ms: float) -> dict:
        """Build structured metadata for storage."""
        meta = {
            "spec_id": handoff.spec_id,
            "sequence": handoff.sequence,
            "source_agent": handoff.source_agent,
            "target_agent": handoff.target_agent,
            "outcome": handoff.status.outcome,
            "timestamp": handoff.metadata.timestamp,
            "processing_time_ms": processing_time_ms,
            "idempotency_key": handoff.idempotency_key,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        # Deliverables metadata
        if handoff.deliverables:
            meta["files_count"] = len(handoff.files_touched)
            meta["typecheck_passed"] = handoff.deliverables.typecheckPassed
            if handoff.deliverables.testsStatus:
                ts = handoff.deliverables.testsStatus
                meta["tests_passed"] = ts.passed
                meta["tests_failed"] = ts.failed
                meta["tests_new"] = ts.newTests
        # Review findings metadata
        if handoff.status.reviewFindings:
            rf = handoff.status.reviewFindings
            meta["blocking_count"] = len(rf.blocking)
            meta["medium_count"] = len(rf.medium)
            meta["low_count"] = len(rf.low)
        # Evidence flags
        if handoff.evidence:
            meta["has_security_review"] = handoff.evidence.securityCheck is not None
            meta["has_code_quality_review"] = handoff.evidence.codeQuality is not None
        # Deviations and known issues
        meta["deviations_count"] = len(handoff.status.deviationsFromPlan)
        meta["known_issues_count"] = len(handoff.nextAgent.knownIssues)
        meta["goal_met"] = handoff.status.goalMet
        return meta

    def _embed(self, text: str) -> Optional[list[float]]:
        """Get embedding from Ollama. Returns None on failure."""
        try:
            import httpx
            with httpx.Client(timeout=15) as client:
                response = client.post(
                    f"{self.config.ollama_url}/api/embeddings",
                    json={
                        "model": self.config.ollama_embed_model,
                        "prompt": text,
                    },
                )
                response.raise_for_status()
                return response.json()["embedding"]
        except Exception as e:
            log.warning(f"Ollama embedding failed: {e}")
            return None

    def _store_to_qdrant(self, summary: str, metadata: dict):
        """Embed and store to Qdrant."""
        vector = self._embed(summary)
        if not vector:
            log.warning("Skipping Qdrant store — embedding failed")
            return

        from qdrant_client.models import PointStruct
        point_id = str(uuid.uuid4())
        self._qdrant.upsert(
            collection_name=COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={"summary": summary, **metadata},
                )
            ],
        )
        log.info(f"Stored to Qdrant: {metadata['spec_id']} #{metadata['sequence']}")

    def _store_to_neo4j(self, handoff: "Handoff", metadata: dict, summary: str):
        """Store handoff event and relationships to Neo4j."""
        with self._neo4j_driver.session() as session:
            # Create HandoffEvent node with enriched metadata
            session.run(
                """
                MERGE (h:HandoffEvent {idempotency_key: $key})
                ON CREATE SET
                    h.spec_id = $spec_id,
                    h.sequence = $sequence,
                    h.source_agent = $source,
                    h.target_agent = $target,
                    h.outcome = $outcome,
                    h.timestamp = $timestamp,
                    h.processing_time_ms = $proc_time,
                    h.summary = $summary,
                    h.files_count = $files_count,
                    h.tests_passed = $tests_passed,
                    h.tests_failed = $tests_failed,
                    h.typecheck_passed = $typecheck_passed,
                    h.blocking_count = $blocking_count,
                    h.deviations_count = $deviations_count,
                    h.goal_met = $goal_met,
                    h.stored_at = datetime()
                """,
                key=metadata["idempotency_key"],
                spec_id=metadata["spec_id"],
                sequence=metadata["sequence"],
                source=metadata["source_agent"],
                target=metadata["target_agent"],
                outcome=metadata["outcome"],
                timestamp=metadata["timestamp"],
                proc_time=metadata["processing_time_ms"],
                summary=summary,
                files_count=metadata.get("files_count", 0),
                tests_passed=metadata.get("tests_passed"),
                tests_failed=metadata.get("tests_failed"),
                typecheck_passed=metadata.get("typecheck_passed"),
                blocking_count=metadata.get("blocking_count", 0),
                deviations_count=metadata.get("deviations_count", 0),
                goal_met=metadata.get("goal_met"),
            )

            # Link sequential handoffs: previous → current
            if metadata["sequence"] > 1:
                session.run(
                    """
                    MATCH (prev:HandoffEvent {spec_id: $spec_id, sequence: $prev_seq})
                    MATCH (curr:HandoffEvent {spec_id: $spec_id, sequence: $curr_seq})
                    MERGE (prev)-[:NEXT_HANDOFF]->(curr)
                    """,
                    spec_id=metadata["spec_id"],
                    prev_seq=metadata["sequence"] - 1,
                    curr_seq=metadata["sequence"],
                )

            # Link to Specification node if it exists
            session.run(
                """
                MATCH (s:Specification {id: $spec_id})
                MATCH (h:HandoffEvent {idempotency_key: $key})
                MERGE (s)-[:HAS_HANDOFF]->(h)
                """,
                spec_id=metadata["spec_id"],
                key=metadata["idempotency_key"],
            )

            log.info(f"Stored to Neo4j: {metadata['spec_id']} #{metadata['sequence']}")
