"""Handoff JSON parser with Pydantic validation."""

from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import Optional


class HandoffMetadata(BaseModel):
    specId: str
    agent: str
    timestamp: str
    sequence: int


class TestsStatus(BaseModel):
    """Test suite execution results."""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    newTests: Optional[int] = None
    output: Optional[str] = None


class ReviewFindings(BaseModel):
    """Categorized review findings by severity."""
    blocking: list[str] = []
    medium: list[str] = []
    low: list[str] = []


class HandoffStatus(BaseModel):
    outcome: str  # "success" | "failed" | "approved"
    issueCount: int = 0
    claimMismatches: int = 0
    highSeverity: Optional[int] = None
    notes: Optional[str] = None
    goalMet: Optional[bool] = None
    unresolvedIssues: list[str] = []
    deviationsFromPlan: list[str] = []
    reviewFindings: Optional[ReviewFindings] = None


class Deliverables(BaseModel):
    """Files and verification results from agent work."""
    filesCreated: list[str] = []
    filesModified: list[str] = []
    filesReviewed: list[str] = []
    testsStatus: Optional[TestsStatus] = None
    typecheckPassed: Optional[bool] = None
    lintPassed: Optional[bool] = None
    buildPassed: Optional[bool] = None

    @field_validator('typecheckPassed', 'lintPassed', 'buildPassed', mode='before')
    @classmethod
    def coerce_check_status(cls, v):
        """Coerce string status values to booleans.

        Tester agents sometimes write strings like 'not run' instead of
        null/boolean. This validator normalises them so the pipeline
        doesn't stall on a Pydantic validation error.
        """
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, str):
            lowered = v.strip().lower()
            if lowered in ('not run', 'not executed', 'skipped', 'n/a', ''):
                return None
            if lowered in ('true', 'passed', 'pass', 'yes', 'ok'):
                return True
            if lowered in ('false', 'failed', 'fail', 'no', 'error'):
                return False
            # Unknown string — treat as None rather than crashing
            return None
        return v


class Evidence(BaseModel):
    """Reviewer evidence and verification details."""
    reviewAreas: Optional[dict] = None
    securityCheck: Optional[str] = None
    codeQuality: Optional[str] = None


class NextAgent(BaseModel):
    target: str
    action: str
    context: Optional[str] = None
    knownIssues: list[str] = []


class Handoff(BaseModel):
    """Validated handoff document matching production JSON structure."""
    metadata: HandoffMetadata
    status: HandoffStatus
    nextAgent: NextAgent

    # Structured deliverables and evidence
    deliverables: Optional[Deliverables] = None
    evidence: Optional[Evidence] = None
    changeSummary: Optional[dict] = None

    # Legacy fields that may or may not be present
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

    @property
    def files_touched(self) -> list[str]:
        """All files involved in this handoff (created + modified + reviewed)."""
        if not self.deliverables:
            return []
        return list(set(
            self.deliverables.filesCreated
            + self.deliverables.filesModified
            + self.deliverables.filesReviewed
        ))

    @property
    def test_summary(self) -> Optional[str]:
        """One-line test result summary."""
        if not self.deliverables or not self.deliverables.testsStatus:
            return None
        ts = self.deliverables.testsStatus
        parts = [f"{ts.passed} passed"]
        if ts.failed:
            parts.append(f"{ts.failed} failed")
        if ts.skipped:
            parts.append(f"{ts.skipped} skipped")
        if ts.newTests:
            parts.append(f"{ts.newTests} new")
        return ", ".join(parts)

    @property
    def blocking_issues(self) -> list[str]:
        """All blocking issues from review findings + known issues."""
        issues = []
        if self.status.reviewFindings:
            issues.extend(self.status.reviewFindings.blocking)
        return issues
