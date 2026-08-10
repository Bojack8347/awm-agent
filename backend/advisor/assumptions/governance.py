"""Server-owned approval contracts and service for assumption candidates."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionStatus,
    DurableFactPromotionExamination,
    PermittedUse,
    SourceClass,
)
from advisor.assumptions.providers.repository import AssumptionCandidateRepository
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


class GovernanceDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class GovernanceErrorCode(str, Enum):
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    CANDIDATE_REQUIRED = "candidate_required"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    DECISION_CONFLICT = "decision_conflict"
    VARIABLE_NOT_GOVERNED = "variable_not_governed"
    APPROVAL_NOT_REQUIRED = "approval_not_required"
    REVIEWER_REQUIRED = "reviewer_required"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"


class AssumptionGovernanceError(RuntimeError):
    def __init__(
        self,
        code: GovernanceErrorCode,
        message: str,
        *,
        artifact_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.artifact_id = artifact_id


class AssumptionReviewRequest(BaseModel):
    """Small review input; candidate content remains server-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_artifact_id: str = Field(min_length=1, max_length=240)
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: GovernanceDecision
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=240)


class AssumptionDecisionRecord(BaseModel):
    """Append-only audit record for one final candidate decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.assumption_decision.v1"
    decision_id: str = Field(min_length=1, max_length=240)
    candidate_artifact_id: str = Field(min_length=1, max_length=240)
    candidate_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    variable_key: str = Field(min_length=1, max_length=160)
    effective_year: int = Field(ge=2000, le=2200)
    decision: GovernanceDecision
    decided_by: str = Field(min_length=1, max_length=240)
    decided_at: datetime
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=240)
    approved_artifact_id: str | None = Field(default=None, max_length=240)
    approved_version: int | None = Field(default=None, ge=1)
    supersedes_artifact_id: str | None = Field(default=None, max_length=240)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    decision_origin: Literal[
        "human_review",
        "server_verified_promotion",
        "agent_reviewed_promotion",
    ] = "human_review"
    promotion_examination: DurableFactPromotionExamination | None = None

    @model_validator(mode="after")
    def validate_decision_output(self) -> "AssumptionDecisionRecord":
        if self.decided_at.tzinfo is None:
            raise ValueError("decision time must be timezone-aware")
        has_approval = (
            self.approved_artifact_id is not None
            and self.approved_version is not None
        )
        if self.decision is GovernanceDecision.APPROVE and not has_approval:
            raise ValueError("approval decisions require an approved artifact")
        if self.decision is GovernanceDecision.REJECT and (
            self.approved_artifact_id is not None
            or self.approved_version is not None
            or self.supersedes_artifact_id is not None
        ):
            raise ValueError("rejection decisions cannot create approved artifacts")
        if self.decision_origin in {
            "server_verified_promotion",
            "agent_reviewed_promotion",
        }:
            if (
                self.decision is not GovernanceDecision.APPROVE
                or self.promotion_examination is None
                or self.promotion_examination.decision.status != "promoted"
                or self.promotion_examination.decision.approved_artifact_id
                != self.approved_artifact_id
                or self.promotion_examination.decision.approved_version
                != self.approved_version
            ):
                raise ValueError(
                    "server promotion decisions require a matching examination"
                )
            if self.decision_origin == "agent_reviewed_promotion" and (
                self.promotion_examination.schema_version
                != "awm.durable_fact_promotion.v2"
                or self.promotion_examination.agent_assessment is None
                or self.promotion_examination.agent_assessment.decision
                != "authorize_durable_reuse"
            ):
                raise ValueError(
                    "agent-reviewed promotions require an authorizing assessment"
                )
            if self.decision_origin == "server_verified_promotion" and (
                self.promotion_examination.schema_version
                != "awm.durable_fact_promotion.v1"
                or self.promotion_examination.agent_assessment is not None
            ):
                raise ValueError(
                    "legacy server promotions require a v1 examination without "
                    "an agent assessment"
                )
        elif self.promotion_examination is not None:
            raise ValueError("human review decisions cannot carry promotion evidence")
        return self


class AssumptionCandidateReview(BaseModel):
    """Server-owned candidate content plus its current review state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.assumption_candidate_review.v1"
    candidate: AssumptionArtifact
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    governance_status: str = Field(
        pattern=r"^(pending|approved|rejected)$"
    )
    decision: AssumptionDecisionRecord | None = None

    @model_validator(mode="after")
    def validate_review_state(self) -> "AssumptionCandidateReview":
        if self.candidate.status is not AssumptionStatus.CANDIDATE:
            raise ValueError("candidate review requires an immutable candidate")
        if self.governance_status == "pending" and self.decision is not None:
            raise ValueError("pending candidates cannot have a final decision")
        if self.governance_status != "pending" and self.decision is None:
            raise ValueError("final candidate states require a decision")
        return self


class GovernedAssumptionRepository(
    AssumptionCandidateRepository,
    Protocol,
):
    def get(self, artifact_id: str) -> AssumptionArtifact | None: ...

    def latest_approved(
        self,
        variable_key: str,
        *,
        effective_year: int,
    ) -> AssumptionArtifact | None: ...

    def durable_promotion_available(self) -> bool: ...

    def apply_verified_promotion(
        self,
        *,
        batch: "ProviderCandidateBatch",
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination,
    ) -> AssumptionDecisionRecord: ...

    def apply_decision(
        self,
        *,
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination | None = None,
    ) -> AssumptionDecisionRecord: ...

    def decision_history(
        self,
        candidate_artifact_id: str,
    ) -> tuple[AssumptionDecisionRecord, ...]: ...

    def list_candidates(
        self,
        *,
        variable_key: str | None = None,
        effective_year: int | None = None,
        governance_status: str | None = None,
        limit: int = 100,
    ) -> tuple[AssumptionCandidateReview, ...]: ...


def assumption_artifact_fingerprint(artifact: AssumptionArtifact) -> str:
    """Hash immutable candidate content, excluding mutable governance fields."""

    payload = artifact.model_dump(mode="json")
    for key in (
        "artifact_id",
        "created_at",
        "status",
        "permitted_uses",
        "approved_by",
        "approved_at",
        "assumption_set_id",
        "assumption_set_version",
    ):
        payload.pop(key, None)
    for evidence in payload.get("evidence", []):
        evidence.pop("retrieved_at", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_approved_assumption(
    *,
    candidate: AssumptionArtifact,
    reviewer_id: str,
    decided_at: datetime,
    approved_version: int,
    approved_uses: tuple[PermittedUse, ...],
) -> AssumptionArtifact:
    if candidate.effective_from is None:
        raise AssumptionGovernanceError(
            GovernanceErrorCode.CANDIDATE_REQUIRED,
            "candidate effective period is required for approval",
            artifact_id=candidate.artifact_id,
        )
    fingerprint = assumption_artifact_fingerprint(candidate)
    effective_year = candidate.effective_from.year
    return AssumptionArtifact(
        artifact_id=(
            f"approved:{candidate.variable_key}:{effective_year}:"
            f"v{approved_version}:{fingerprint[:16]}"
        ),
        variable_key=candidate.variable_key,
        source_class=candidate.source_class,
        value=candidate.value,
        unit=candidate.unit,
        jurisdiction=candidate.jurisdiction,
        effective_from=candidate.effective_from,
        effective_to=candidate.effective_to,
        status=AssumptionStatus.APPROVED,
        permitted_uses=approved_uses,
        assumption_set_id=(
            f"authoritative:{candidate.variable_key}:{effective_year}"
        ),
        assumption_set_version=approved_version,
        evidence=candidate.evidence,
        approved_by=reviewer_id,
        approved_at=decided_at,
        created_at=decided_at,
    )


def build_assumption_decision_record(
    *,
    request: AssumptionReviewRequest,
    candidate: AssumptionArtifact,
    reviewer_id: str,
    decided_at: datetime,
    policy_id: str,
    policy_version: int,
    approved: AssumptionArtifact | None = None,
    supersedes_artifact_id: str | None = None,
    promotion_examination: DurableFactPromotionExamination | None = None,
) -> AssumptionDecisionRecord:
    if candidate.effective_from is None:
        raise AssumptionGovernanceError(
            GovernanceErrorCode.CANDIDATE_REQUIRED,
            "candidate effective period is required for a decision",
            artifact_id=candidate.artifact_id,
        )
    decision_digest = hashlib.sha256(
        request.idempotency_key.encode("utf-8")
    ).hexdigest()
    return AssumptionDecisionRecord(
        decision_id=f"assumption-decision:{decision_digest[:32]}",
        candidate_artifact_id=candidate.artifact_id,
        candidate_fingerprint=assumption_artifact_fingerprint(candidate),
        variable_key=candidate.variable_key,
        effective_year=candidate.effective_from.year,
        decision=request.decision,
        decided_by=reviewer_id,
        decided_at=decided_at,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        approved_artifact_id=approved.artifact_id if approved else None,
        approved_version=approved.assumption_set_version if approved else None,
        supersedes_artifact_id=supersedes_artifact_id,
        policy_id=policy_id,
        policy_version=policy_version,
        decision_origin=(
            "human_review"
            if promotion_examination is None
            else (
                "agent_reviewed_promotion"
                if promotion_examination.agent_assessment is not None
                and promotion_examination.agent_assessment.decision
                == "authorize_durable_reuse"
                else "server_verified_promotion"
            )
        ),
        promotion_examination=promotion_examination,
    )


class AssumptionApprovalService:
    """Validate policy and delegate one atomic final decision to storage."""

    def __init__(
        self,
        *,
        repository: GovernedAssumptionRepository,
        source_policy: VariableSourceRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.source_policy = source_policy or load_variable_source_registry()

    def review(
        self,
        request: AssumptionReviewRequest,
        *,
        reviewer_id: str,
        decided_at: datetime | None = None,
    ) -> AssumptionDecisionRecord:
        reviewer_id = str(reviewer_id or "").strip()
        if not reviewer_id:
            raise AssumptionGovernanceError(
                GovernanceErrorCode.REVIEWER_REQUIRED,
                "authenticated server reviewer identity is required",
                artifact_id=request.candidate_artifact_id,
            )
        decided_at = decided_at or datetime.now(timezone.utc)
        if decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")

        candidate = self.repository.get(request.candidate_artifact_id)
        if candidate is None:
            raise AssumptionGovernanceError(
                GovernanceErrorCode.CANDIDATE_NOT_FOUND,
                "assumption candidate was not found",
                artifact_id=request.candidate_artifact_id,
            )
        if (
            candidate.status is not AssumptionStatus.CANDIDATE
            or candidate.source_class is not SourceClass.PUBLIC_AUTHORITATIVE
        ):
            raise AssumptionGovernanceError(
                GovernanceErrorCode.CANDIDATE_REQUIRED,
                "only public-authoritative candidates can enter this workflow",
                artifact_id=candidate.artifact_id,
            )
        actual_fingerprint = assumption_artifact_fingerprint(candidate)
        if not hmac.compare_digest(
            actual_fingerprint,
            request.expected_fingerprint,
        ):
            raise AssumptionGovernanceError(
                GovernanceErrorCode.FINGERPRINT_MISMATCH,
                "candidate content changed after the review began",
                artifact_id=candidate.artifact_id,
            )

        policy = self.source_policy.get(candidate.variable_key)
        if policy is None:
            raise AssumptionGovernanceError(
                GovernanceErrorCode.VARIABLE_NOT_GOVERNED,
                "candidate variable is not registered",
                artifact_id=candidate.artifact_id,
            )
        if not policy.approval_required:
            raise AssumptionGovernanceError(
                GovernanceErrorCode.APPROVAL_NOT_REQUIRED,
                "candidate variable is not governed by this approval workflow",
                artifact_id=candidate.artifact_id,
            )

        approved_uses = tuple(
            use
            for use in (
                PermittedUse.REPORTING,
                PermittedUse.MODEL_INPUT,
            )
            if use in policy.allowed_uses
        )
        return self.repository.apply_decision(
            request=request,
            reviewer_id=reviewer_id,
            decided_at=decided_at,
            policy_id=self.source_policy.document.policy_id,
            policy_version=self.source_policy.document.policy_version,
            approved_uses=approved_uses,
        )
