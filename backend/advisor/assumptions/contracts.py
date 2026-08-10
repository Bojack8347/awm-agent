"""Typed contracts for variable sourcing and reusable planning assumptions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceClass(str, Enum):
    CLIENT_SPECIFIC = "client_specific"
    DETERMINISTICALLY_DERIVED = "deterministically_derived"
    PUBLIC_AUTHORITATIVE = "public_authoritative"
    GOVERNED_PLANNING_ASSUMPTION = "governed_planning_assumption"


class PermittedUse(str, Enum):
    REPORTING = "reporting"
    MODEL_INPUT = "model_input"
    RECOMMENDATION = "recommendation"


class MissingBehavior(str, Enum):
    ASK_CLIENT = "ask_client"
    USE_CONNECTED_SOURCE = "use_connected_source"
    DERIVE_DETERMINISTICALLY = "derive_deterministically"
    USE_APPROVED_ASSUMPTION = "use_approved_assumption"
    USE_APPROVED_SCENARIOS = "use_approved_scenarios"
    STOP = "stop"


class AssumptionStatus(str, Enum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class VariableSourcePolicy(BaseModel):
    """Expanded policy for one canonical planning variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str = Field(min_length=1, max_length=160)
    source_class: SourceClass
    allowed_sources: tuple[str, ...] = Field(min_length=1)
    online_research_allowed: bool
    default_allowed: bool
    client_authorization_required: bool
    approval_required: bool
    session_use_allowed: bool = False
    automatic_promotion_allowed: bool = False
    maximum_age_days: int | None = Field(default=None, ge=0)
    allowed_uses: tuple[PermittedUse, ...] = Field(min_length=1)
    missing_behavior: MissingBehavior
    notes: str = Field(default="", max_length=1200)


class VariablePolicyGroup(BaseModel):
    """Compact file representation shared by variables with identical policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_keys: tuple[str, ...] = Field(min_length=1)
    source_class: SourceClass
    allowed_sources: tuple[str, ...] = Field(min_length=1)
    online_research_allowed: bool
    default_allowed: bool
    client_authorization_required: bool
    approval_required: bool
    session_use_allowed: bool = False
    automatic_promotion_allowed: bool = False
    maximum_age_days: int | None = Field(default=None, ge=0)
    allowed_uses: tuple[PermittedUse, ...] = Field(min_length=1)
    missing_behavior: MissingBehavior
    notes: str = Field(default="", max_length=1200)

    def expand(self) -> tuple[VariableSourcePolicy, ...]:
        shared = self.model_dump(exclude={"variable_keys"})
        return tuple(
            VariableSourcePolicy(variable_key=variable_key, **shared)
            for variable_key in self.variable_keys
        )


class VariableSourcePolicyDocument(BaseModel):
    """Versioned source-policy registry loaded from the repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.variable_source_policy.v1"
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    effective_date: str = Field(min_length=10, max_length=10)
    enforcement_mode: str = Field(pattern=r"^(observe_only|enforced)$")
    source_aliases: dict[str, str]
    groups: tuple[VariablePolicyGroup, ...] = Field(min_length=1)


class AssumptionEvidence(BaseModel):
    """One source supporting a candidate or approved assumption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=240)
    source_type: str = Field(min_length=1, max_length=80)
    publisher: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    published_at: datetime | None = None
    retrieved_at: datetime
    content_hash: str | None = Field(default=None, max_length=128)


class AssumptionArtifact(BaseModel):
    """Durable, typed assumption value; approval is server-owned."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "awm.assumption_artifact.v1"
    artifact_id: str = Field(min_length=1, max_length=240)
    variable_key: str = Field(min_length=1, max_length=160)
    source_class: SourceClass
    value: Any
    unit: str = Field(min_length=1, max_length=80)
    jurisdiction: str | None = Field(default=None, max_length=160)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    status: AssumptionStatus
    permitted_uses: tuple[PermittedUse, ...] = Field(min_length=1)
    assumption_set_id: str | None = Field(default=None, max_length=240)
    assumption_set_version: int | None = Field(default=None, ge=1)
    evidence: tuple[AssumptionEvidence, ...] = ()
    approved_by: str | None = Field(default=None, max_length=240)
    approved_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_authority(self) -> "AssumptionArtifact":
        if (
            self.source_class is SourceClass.PUBLIC_AUTHORITATIVE
            and not self.evidence
        ):
            raise ValueError("public authoritative assumptions require evidence")
        if self.status is AssumptionStatus.APPROVED and (
            not self.approved_by or self.approved_at is None
        ):
            raise ValueError("approved assumptions require server-owned approval metadata")
        if (
            PermittedUse.RECOMMENDATION in self.permitted_uses
            and self.status is not AssumptionStatus.APPROVED
        ):
            raise ValueError("recommendation use requires an approved assumption")
        if (self.assumption_set_id is None) != (self.assumption_set_version is None):
            raise ValueError(
                "assumption_set_id and assumption_set_version must be supplied together"
            )
        return self


class DurableFactPromotionCandidate(BaseModel):
    """Value-free reference to the researched fact and verified candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str | None = Field(default=None, max_length=240)
    artifact_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    finding_content_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    canonical_value_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    variable_key: str = Field(min_length=1, max_length=160)
    effective_year: int = Field(ge=2000, le=2200)
    effective_from: datetime
    effective_to: datetime
    unit: str = Field(min_length=1, max_length=80)
    jurisdiction: str = Field(min_length=1, max_length=160)
    researched_at: datetime
    session_expires_at: datetime


class DurableFactPromotionVerification(BaseModel):
    """Independent server-owned evidence used to examine promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["authoritative_snapshot_match.v1"] = (
        "authoritative_snapshot_match.v1"
    )
    provider_id: str = Field(min_length=1, max_length=80)
    verifier_version: str = Field(min_length=1, max_length=40)
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_value_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verified_at: datetime
    evidence: tuple[AssumptionEvidence, ...] = Field(min_length=1)


class DurableFactAgentAssessment(BaseModel):
    """Server-stamped agent decision about durable storage and reuse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["awm.durable_fact_agent_assessment.v1"] = (
        "awm.durable_fact_agent_assessment.v1"
    )
    assessment_id: str = Field(
        pattern=r"^durable-fact-agent-assessment:[a-f0-9]{32}$"
    )
    assessed_by: str = Field(
        min_length=7,
        max_length=240,
        pattern=r"^agent:[a-zA-Z0-9][a-zA-Z0-9._:/-]*$",
    )
    assessed_at: datetime
    finding_content_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    decision: Literal[
        "authorize_durable_reuse",
        "keep_session_only",
    ]
    reason_code: Literal[
        "authoritative_fact_reuse_appropriate",
        "authoritative_fact_reuse_not_appropriate",
    ]

    @model_validator(mode="after")
    def validate_decision_reason(self) -> "DurableFactAgentAssessment":
        if self.assessed_at.tzinfo is None:
            raise ValueError("agent assessment time must be timezone-aware")
        expected_reason = (
            "authoritative_fact_reuse_appropriate"
            if self.decision == "authorize_durable_reuse"
            else "authoritative_fact_reuse_not_appropriate"
        )
        if self.reason_code != expected_reason:
            raise ValueError("agent assessment reason must match its decision")
        return self


class DurableFactPromotionPolicy(BaseModel):
    """Exact policy version and least-privilege uses examined by the server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    automatic_promotion_allowed: bool
    maximum_age_days: int | None = Field(default=None, ge=0)
    granted_uses: tuple[PermittedUse, ...] = ()


class DurableFactPromotionExpectedActive(BaseModel):
    """Optimistic-concurrency state examined before an atomic activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str | None = Field(default=None, max_length=240)
    artifact_version: int | None = Field(default=None, ge=1)
    artifact_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_all_or_none(self) -> "DurableFactPromotionExpectedActive":
        supplied = (
            self.artifact_id is not None,
            self.artifact_version is not None,
            self.artifact_fingerprint is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("expected active identity must be complete")
        return self


class DurableFactPromotionChecks(BaseModel):
    """Deterministic checks; these authorize storage, not semantic routing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_integrity: bool
    session_current: bool
    policy_authorized: bool
    source_authority: bool
    dimensional_contract: bool
    value_shape: bool
    independent_value_match: bool
    durable_persistence: bool
    active_state_unchanged: bool


class DurableFactPromotionDecision(BaseModel):
    """Final examination disposition returned after durable commit or refusal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["promoted", "already_current", "session_only"]
    reason_codes: tuple[str, ...] = ()
    approved_artifact_id: str | None = Field(default=None, max_length=240)
    approved_version: int | None = Field(default=None, ge=1)
    supersedes_artifact_id: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_decision(self) -> "DurableFactPromotionDecision":
        has_approved = (
            self.approved_artifact_id is not None
            and self.approved_version is not None
        )
        if self.status in {"promoted", "already_current"}:
            if not has_approved or self.reason_codes:
                raise ValueError("durable decisions require an artifact and no refusal")
        elif has_approved or self.supersedes_artifact_id is not None:
            raise ValueError("session-only decisions cannot authorize durable state")
        elif not self.reason_codes:
            raise ValueError("session-only decisions require a reason")
        return self


class DurableFactPromotionExamination(BaseModel):
    """Server-owned validation of an agent's durable fact-reuse decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "awm.durable_fact_promotion.v1",
        "awm.durable_fact_promotion.v2",
    ] = (
        "awm.durable_fact_promotion.v2"
    )
    examination_id: str = Field(
        pattern=r"^durable-fact-examination:[a-f0-9]{32}$"
    )
    idempotency_key: str = Field(min_length=8, max_length=240)
    examined_at: datetime
    candidate: DurableFactPromotionCandidate
    verification: DurableFactPromotionVerification | None = None
    agent_assessment: DurableFactAgentAssessment | None = None
    policy: DurableFactPromotionPolicy | None = None
    expected_active: DurableFactPromotionExpectedActive
    checks: DurableFactPromotionChecks
    decision: DurableFactPromotionDecision

    @model_validator(mode="after")
    def validate_examination(self) -> "DurableFactPromotionExamination":
        timestamps = (
            self.examined_at,
            self.candidate.effective_from,
            self.candidate.effective_to,
            self.candidate.researched_at,
            self.candidate.session_expires_at,
        )
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("promotion timestamps must be timezone-aware")
        if (
            self.candidate.effective_from.year != self.candidate.effective_year
            or self.candidate.effective_to <= self.candidate.effective_from
            or self.candidate.session_expires_at
            <= self.candidate.researched_at
        ):
            raise ValueError("promotion candidate time window is inconsistent")
        if self.verification is not None and self.verification.verified_at.tzinfo is None:
            raise ValueError("verification time must be timezone-aware")
        if self.agent_assessment is not None and (
            self.agent_assessment.finding_content_sha256
            != self.candidate.finding_content_sha256
            or not (
                self.candidate.researched_at
                <= self.agent_assessment.assessed_at
                <= self.examined_at
            )
        ):
            raise ValueError("agent assessment does not match the examined fact")
        all_checks_pass = all(self.checks.model_dump().values())
        durable = self.decision.status in {"promoted", "already_current"}
        if durable and (
            not all_checks_pass
            or self.verification is None
            or self.policy is None
            or not self.policy.granted_uses
            or self.candidate.artifact_id is None
            or self.candidate.artifact_fingerprint is None
            or self.policy.automatic_promotion_allowed is not True
            or (
                self.schema_version == "awm.durable_fact_promotion.v2"
                and (
                    self.agent_assessment is None
                    or self.agent_assessment.decision
                    != "authorize_durable_reuse"
                )
            )
            or self.verification.canonical_value_sha256
            != self.candidate.canonical_value_sha256
            or PermittedUse.RECOMMENDATION in self.policy.granted_uses
            or not {
                PermittedUse.REPORTING,
                PermittedUse.MODEL_INPUT,
            }.issubset(self.policy.granted_uses)
            or not (
                self.candidate.researched_at
                <= self.examined_at
                < self.candidate.session_expires_at
            )
        ):
            raise ValueError("durable decisions require complete passing examination")
        if not durable and self.policy is not None and self.policy.granted_uses:
            raise ValueError("session-only examinations cannot grant durable uses")
        if self.decision.status == "promoted" and (
            self.decision.supersedes_artifact_id
            != self.expected_active.artifact_id
        ):
            raise ValueError("promotion supersession must match examined active state")
        if self.decision.status == "promoted" and self.decision.approved_version != (
            (self.expected_active.artifact_version or 0) + 1
        ):
            raise ValueError("promotion version must follow examined active state")
        if self.decision.status == "already_current" and (
            self.decision.approved_artifact_id != self.expected_active.artifact_id
            or self.decision.approved_version
            != self.expected_active.artifact_version
        ):
            raise ValueError("current decision must match examined active state")
        return self


class SourceUseDecision(BaseModel):
    """Deterministic compatibility decision for one variable/source pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str
    policy_version: int
    enforcement_mode: str
    variable_key: str
    source_id: str
    normalized_source: str
    source_class: SourceClass | None
    requested_use: PermittedUse
    status: str = Field(pattern=r"^(compatible|incompatible|unclassified)$")
    allowed: bool
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
