"""Process-local governed assumption repository.

This is the database-free fallback for the PostgreSQL implementation. It shares
the same atomic decision contract and never exposes approval to an agent.
"""

from __future__ import annotations

from datetime import datetime

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionEvidence,
    AssumptionStatus,
    DurableFactPromotionExamination,
    PermittedUse,
)
from advisor.assumptions.governance import (
    AssumptionCandidateReview,
    AssumptionDecisionRecord,
    AssumptionGovernanceError,
    AssumptionReviewRequest,
    GovernanceDecision,
    GovernanceErrorCode,
    assumption_artifact_fingerprint,
    build_approved_assumption,
    build_assumption_decision_record,
)
from advisor.assumptions.providers.repository import (
    InMemoryAssumptionCandidateRepository,
)
from advisor.assumptions.providers.contracts import ProviderCandidateBatch


class InMemoryGovernedAssumptionRepository(
    InMemoryAssumptionCandidateRepository
):
    """Thread-safe candidate, decision, and approved-version fallback."""

    def __init__(
        self,
        artifacts: tuple[AssumptionArtifact, ...] = (),
        decisions: tuple[AssumptionDecisionRecord, ...] = (),
    ) -> None:
        super().__init__(artifacts)
        self._decisions_by_idempotency = {
            decision.idempotency_key: decision for decision in decisions
        }
        self._decision_ids_by_candidate: dict[str, list[str]] = {}
        self._decisions_by_id = {
            decision.decision_id: decision for decision in decisions
        }
        for decision in decisions:
            self._decision_ids_by_candidate.setdefault(
                decision.candidate_artifact_id,
                [],
            ).append(decision.decision_id)

    def get(self, artifact_id: str) -> AssumptionArtifact | None:
        with self._lock:
            return self._by_id.get(artifact_id)

    def latest_approved(
        self,
        variable_key: str,
        *,
        effective_year: int,
    ) -> AssumptionArtifact | None:
        with self._lock:
            artifacts = [
                artifact
                for artifact in self._by_id.values()
                if artifact.variable_key == variable_key
                and artifact.status is AssumptionStatus.APPROVED
                and artifact.effective_from is not None
                and artifact.effective_from.year == effective_year
            ]
            if not artifacts:
                return None
            return max(
                artifacts,
                key=lambda artifact: (
                    artifact.assumption_set_version or 0,
                    artifact.created_at,
                ),
            )

    def durable_promotion_available(self) -> bool:
        """Process-local fallback must never be reported as durable storage."""

        return False

    def apply_verified_promotion(
        self,
        *,
        batch: ProviderCandidateBatch,
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination,
    ) -> AssumptionDecisionRecord:
        """Persist the verified candidate and its decision as one local unit."""

        artifacts = batch.artifacts
        verification = promotion_examination.verification
        if (
            len(artifacts) != 1
            or artifacts[0].artifact_id != request.candidate_artifact_id
            or verification is None
            or verification.provider_id != batch.provider_id
            or verification.source_snapshot_sha256 != batch.snapshot_sha256
        ):
            raise AssumptionGovernanceError(
                GovernanceErrorCode.DECISION_CONFLICT,
                "promotion batch does not match the examination",
                artifact_id=request.candidate_artifact_id,
            )
        with self._lock:
            saved_by_id = dict(self._by_id)
            saved_by_variable = {
                key: list(value) for key, value in self._by_variable.items()
            }
            saved_decisions_by_idempotency = dict(
                self._decisions_by_idempotency
            )
            saved_decision_ids_by_candidate = {
                key: list(value)
                for key, value in self._decision_ids_by_candidate.items()
            }
            saved_decisions_by_id = dict(self._decisions_by_id)
            try:
                self.save_batch(batch)
                return self.apply_decision(
                    request=request,
                    reviewer_id=reviewer_id,
                    decided_at=decided_at,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    approved_uses=approved_uses,
                    promotion_examination=promotion_examination,
                )
            except Exception:
                self._by_id = saved_by_id
                self._by_variable = saved_by_variable
                self._decisions_by_idempotency = (
                    saved_decisions_by_idempotency
                )
                self._decision_ids_by_candidate = (
                    saved_decision_ids_by_candidate
                )
                self._decisions_by_id = saved_decisions_by_id
                raise

    def save_batch(self, batch: ProviderCandidateBatch) -> None:
        super().save_batch(batch)
        with self._lock:
            for candidate in batch.artifacts:
                approved = self.latest_approved(
                    candidate.variable_key,
                    effective_year=batch.effective_year,
                )
                if approved is None:
                    continue
                if (
                    assumption_artifact_fingerprint(approved)
                    != assumption_artifact_fingerprint(candidate)
                ):
                    # Approved and candidate IDs differ, so compare the
                    # immutable sourced value/evidence through the decision
                    # lineage instead of replacing either artifact.
                    decisions = self.decision_history(candidate.artifact_id)
                    if not any(
                        decision.approved_artifact_id == approved.artifact_id
                        for decision in decisions
                    ):
                        continue
                refreshed = approved.model_copy(
                    update={
                        "evidence": tuple(
                            AssumptionEvidence.model_validate(
                                {
                                    **evidence.model_dump(),
                                    "retrieved_at": batch.retrieved_at,
                                }
                            )
                            for evidence in approved.evidence
                        )
                    }
                )
                self._by_id[approved.artifact_id] = refreshed

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
    ) -> AssumptionDecisionRecord:
        with self._lock:
            prior_idempotent = self._decisions_by_idempotency.get(
                request.idempotency_key
            )
            if prior_idempotent is not None:
                if (
                    prior_idempotent.candidate_artifact_id
                    == request.candidate_artifact_id
                    and prior_idempotent.candidate_fingerprint
                    == request.expected_fingerprint
                    and prior_idempotent.decision is request.decision
                    and prior_idempotent.reason == request.reason
                    and prior_idempotent.decided_by == reviewer_id
                    and (
                        (
                            prior_idempotent.promotion_examination is None
                            and promotion_examination is None
                        )
                        or (
                            prior_idempotent.promotion_examination is not None
                            and promotion_examination is not None
                            and prior_idempotent.promotion_examination.examination_id
                            == promotion_examination.examination_id
                        )
                    )
                ):
                    return prior_idempotent
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.DECISION_CONFLICT,
                    "idempotency key was already used for another decision",
                    artifact_id=request.candidate_artifact_id,
                )

            prior_decisions = self.decision_history(
                request.candidate_artifact_id
            )
            if prior_decisions:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.DECISION_CONFLICT,
                    "candidate already has a final decision",
                    artifact_id=request.candidate_artifact_id,
                )
            candidate = self._by_id.get(request.candidate_artifact_id)
            if candidate is None:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.CANDIDATE_NOT_FOUND,
                    "assumption candidate was not found",
                    artifact_id=request.candidate_artifact_id,
                )
            if candidate.status is not AssumptionStatus.CANDIDATE:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.CANDIDATE_REQUIRED,
                    "candidate is no longer pending",
                    artifact_id=candidate.artifact_id,
                )
            if (
                assumption_artifact_fingerprint(candidate)
                != request.expected_fingerprint
            ):
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.FINGERPRINT_MISMATCH,
                    "candidate fingerprint changed before persistence",
                    artifact_id=candidate.artifact_id,
                )

            if promotion_examination is not None:
                examined_candidate = promotion_examination.candidate
                if (
                    request.decision is not GovernanceDecision.APPROVE
                    or examined_candidate.artifact_id != candidate.artifact_id
                    or examined_candidate.artifact_fingerprint
                    != request.expected_fingerprint
                    or promotion_examination.policy is None
                    or promotion_examination.policy.policy_id != policy_id
                    or promotion_examination.policy.policy_version
                    != policy_version
                    or tuple(promotion_examination.policy.granted_uses)
                    != tuple(approved_uses)
                ):
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.DECISION_CONFLICT,
                        "promotion examination does not match the candidate decision",
                        artifact_id=candidate.artifact_id,
                    )

            approved: AssumptionArtifact | None = None
            supersedes_artifact_id: str | None = None
            if request.decision is GovernanceDecision.APPROVE:
                current = self.latest_approved(
                    candidate.variable_key,
                    effective_year=candidate.effective_from.year,
                )
                if promotion_examination is not None:
                    expected = promotion_examination.expected_active
                    current_fingerprint = (
                        assumption_artifact_fingerprint(current)
                        if current is not None
                        else None
                    )
                    current_version = (
                        current.assumption_set_version
                        if current is not None
                        else None
                    )
                    if (
                        (current.artifact_id if current else None)
                        != expected.artifact_id
                        or current_version != expected.artifact_version
                        or current_fingerprint != expected.artifact_fingerprint
                    ):
                        raise AssumptionGovernanceError(
                            GovernanceErrorCode.DECISION_CONFLICT,
                            "active assumption changed after promotion examination",
                            artifact_id=candidate.artifact_id,
                        )
                next_version = (
                    (current.assumption_set_version or 0) + 1 if current else 1
                )
                supersedes_artifact_id = current.artifact_id if current else None
                approved = build_approved_assumption(
                    candidate=candidate,
                    reviewer_id=reviewer_id,
                    decided_at=decided_at,
                    approved_version=next_version,
                    approved_uses=approved_uses,
                )
                if promotion_examination is not None and (
                    promotion_examination.decision.approved_artifact_id
                    != approved.artifact_id
                    or promotion_examination.decision.approved_version
                    != approved.assumption_set_version
                    or promotion_examination.decision.supersedes_artifact_id
                    != supersedes_artifact_id
                ):
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.DECISION_CONFLICT,
                        "promotion examination predicted different durable state",
                        artifact_id=candidate.artifact_id,
                    )
            decision = build_assumption_decision_record(
                request=request,
                candidate=candidate,
                reviewer_id=reviewer_id,
                decided_at=decided_at,
                policy_id=policy_id,
                policy_version=policy_version,
                approved=approved,
                supersedes_artifact_id=supersedes_artifact_id,
                promotion_examination=promotion_examination,
            )
            if approved is not None:
                self._save(approved)
            self._decisions_by_id[decision.decision_id] = decision
            self._decisions_by_idempotency[decision.idempotency_key] = decision
            self._decision_ids_by_candidate.setdefault(
                candidate.artifact_id,
                [],
            ).append(decision.decision_id)
            return decision

    def latest(
        self,
        variable_key: str,
        *,
        effective_year: int | None = None,
    ) -> AssumptionArtifact | None:
        artifact = super().latest(
            variable_key,
            effective_year=effective_year,
        )
        if artifact is None or artifact.status is not AssumptionStatus.CANDIDATE:
            return artifact
        decisions = self.decision_history(artifact.artifact_id)
        if not decisions:
            return artifact
        if decisions[-1].decision is GovernanceDecision.REJECT:
            return artifact.model_copy(update={"status": AssumptionStatus.REJECTED})
        approved = (
            self._by_id.get(decisions[-1].approved_artifact_id)
            if decisions[-1].approved_artifact_id
            else None
        )
        return approved or artifact

    def decision_history(
        self,
        candidate_artifact_id: str,
    ) -> tuple[AssumptionDecisionRecord, ...]:
        with self._lock:
            return tuple(
                self._decisions_by_id[decision_id]
                for decision_id in self._decision_ids_by_candidate.get(
                    candidate_artifact_id,
                    [],
                )
            )

    def list_candidates(
        self,
        *,
        variable_key: str | None = None,
        effective_year: int | None = None,
        governance_status: str | None = None,
        limit: int = 100,
    ) -> tuple[AssumptionCandidateReview, ...]:
        """List immutable candidates without exposing generated approvals."""

        if governance_status not in {None, "pending", "approved", "rejected"}:
            raise ValueError("unsupported governance_status")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            reviews: list[AssumptionCandidateReview] = []
            for candidate in self._by_id.values():
                if candidate.status is not AssumptionStatus.CANDIDATE:
                    continue
                if variable_key and candidate.variable_key != variable_key:
                    continue
                if (
                    effective_year is not None
                    and (
                        candidate.effective_from is None
                        or candidate.effective_from.year != effective_year
                    )
                ):
                    continue
                decisions = self.decision_history(candidate.artifact_id)
                decision = decisions[-1] if decisions else None
                current_status = (
                    (
                        "approved"
                        if decision.decision is GovernanceDecision.APPROVE
                        else "rejected"
                    )
                    if decision is not None
                    else "pending"
                )
                if (
                    governance_status is not None
                    and current_status != governance_status
                ):
                    continue
                reviews.append(
                    AssumptionCandidateReview(
                        candidate=candidate,
                        fingerprint=assumption_artifact_fingerprint(candidate),
                        governance_status=current_status,
                        decision=decision,
                    )
                )
            reviews.sort(
                key=lambda review: (
                    review.candidate.created_at,
                    review.candidate.artifact_id,
                ),
                reverse=True,
            )
            return tuple(reviews[:limit])
