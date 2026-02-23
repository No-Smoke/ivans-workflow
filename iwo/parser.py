"""Handoff JSON parser with Pydantic validation.

Handles format variations across the 6-agent Boris pipeline:
- metadata.specId (planner, docs) vs metadata.spec (builder, reviewer, tester, deployer)
- deliverables as array (builder) vs dict (planner, docs) vs absent (reviewer, tester, deployer)
- Agent-specific extra fields silently ignored (findings, deployment, testResults, etc.)
"""

from datetime import datetime
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Any, Optional


class HandoffMetadata(BaseModel):
    """Handoff metadata — accepts both 'specId' and 'spec' as the spec identifier."""
    model_config = ConfigDict(extra="ignore")

    specId: str = Field(validation_alias=AliasChoices("specId", "spec"))
    agent: str
    timestamp: str
    sequence: int
    received_at: Optional[str] = None


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
    model_config = ConfigDict(extra="ignore")

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
    model_config = ConfigDict(extra="ignore")

    filesCreated: list[str] = []
    filesModified: list[str] = []
    filesReviewed: list[str] = []
    testsStatus: Optional[TestsStatus] = None
    typecheckPassed: Optional[bool] = None


class Evidence(BaseModel):
    """Reviewer evidence and verification details."""
    reviewAreas: Optional[dict] = None
    securityCheck: Optional[str] = None
    codeQuality: Optional[str] = None


class NextAgent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: str
    action: str
    context: Optional[str] = None
    knownIssues: list[str] = []


class Handoff(BaseModel):
    """Validated handoff document matching production JSON structure.

    Handles format variations across different agents:
    - metadata.specId vs metadata.spec (both accepted via AliasChoices)
    - deliverables as array (builder) vs dict (planner/docs) vs absent
    - Extra agent-specific top-level fields silently ignored
    """
    model_config = ConfigDict(extra="ignore")

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

    @model_validator(mode="before")
    @classmethod
    def _normalize_deliverables(cls, data: Any) -> Any:
        """Normalize builder's array deliverables into Deliverables dict format.

        Builder writes deliverables as:
            [{file: "...", action: "created|modified", ...}, ...]
        Other agents write as:
            {filesCreated: [...], filesModified: [...], ...}
        """
        if isinstance(data, dict):
            deliverables = data.get("deliverables")
            if isinstance(deliverables, list):
                created = []
                modified = []
                for item in deliverables:
                    if isinstance(item, dict):
                        filepath = item.get("file", "")
                        if not filepath:
                            continue
                        if item.get("action") == "created":
                            created.append(filepath)
                        else:
                            modified.append(filepath)
                data["deliverables"] = {
                    "filesCreated": created,
                    "filesModified": modified,
                }
        return data

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
