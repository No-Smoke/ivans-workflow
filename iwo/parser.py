"""Handoff JSON parser with Pydantic validation."""

from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


class HandoffMetadata(BaseModel):
    specId: str
    agent: str
    timestamp: str
    sequence: int


class HandoffStatus(BaseModel):
    outcome: str  # "success" | "failed"
    issueCount: int = 0
    claimMismatches: int = 0
    highSeverity: Optional[int] = None
    notes: Optional[str] = None


class NextAgent(BaseModel):
    target: str
    action: str
    context: Optional[str] = None


class Handoff(BaseModel):
    """Validated handoff document matching production JSON structure."""
    metadata: HandoffMetadata
    status: HandoffStatus
    nextAgent: NextAgent

    # Fields that may or may not be present
    reviewDetails: Optional[dict] = None
    claimVerification: Optional[dict] = None
    summary: Optional[dict] = None

    @property
    def spec_id(self) -> str:
        return self.metadata.specId

    @property
    def source_agent(self) -> str:
        return self.metadata.agent

    @property
    def target_agent(self) -> str:
        return self.nextAgent.target

    @property
    def sequence(self) -> int:
        return self.metadata.sequence

    @property
    def is_rejection(self) -> bool:
        return self.status.outcome == "failed"

    @property
    def idempotency_key(self) -> str:
        """Unique key to prevent duplicate processing."""
        return f"{self.spec_id}:{self.sequence}:{self.source_agent}:{self.target_agent}"
