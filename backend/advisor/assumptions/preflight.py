"""Demand-driven deterministic acquisition for required public assumptions."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from advisor.assumptions.contracts import SourceClass
from advisor.assumptions.governance import (
    AssumptionGovernanceError,
    GovernedAssumptionRepository,
    GovernanceDecision,
)
from advisor.assumptions.providers.contracts import (
    ProviderAdapterError,
    RefreshReason,
)
from advisor.assumptions.providers.refresh import ProviderRefreshService
from advisor.assumptions.providers.registry import AuthoritativeProviderRegistry
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


class PreflightStatus(str, Enum):
    EXISTING_SOURCE = "existing_source"
    CURRENT_APPROVED = "current_approved"
    PENDING_REVIEW = "pending_review"
    CANDIDATE_CREATED = "candidate_created"
    APPROVED_REVERIFIED = "approved_reverified"
    REJECTED_OR_SUPERSEDED = "rejected_or_superseded"
    UNSUPPORTED = "unsupported"
    INELIGIBLE = "ineligible"
    FAILED = "failed"
    RESEARCH_CANDIDATE_CREATED = "research_candidate_created"


class ProjectionAssumptionPreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str
    effective_year: int
    provider_id: str | None = None
    status: PreflightStatus
    refresh_reason: RefreshReason | None = None
    approved_artifact_id: str | None = None
    candidate_artifact_id: str | None = None
    current_value_preserved: bool = True
    model_inputs_changed: bool = False
    research_attempted: bool = False
    error_code: str | None = None


class ProjectionAssumptionPreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.projection_assumption_preflight.v1"
    status: Literal["complete", "partial"]
    effective_year: int
    required_variables: tuple[str, ...]
    checks: tuple[ProjectionAssumptionPreflightCheck, ...]
    model_inputs_changed: bool = False
    research_attempted: bool = False


class ProjectionAssumptionPreflightService:
    """Acquire provider candidates without approving or activating a value."""

    def __init__(
        self,
        *,
        providers: AuthoritativeProviderRegistry,
        repository: GovernedAssumptionRepository,
        source_policy: VariableSourceRegistry | None = None,
    ) -> None:
        self.providers = providers
        self.repository = repository
        self.source_policy = source_policy or load_variable_source_registry()
        self.refresh_service = ProviderRefreshService(
            providers=providers,
            repository=repository,
            source_policy=self.source_policy,
        )

    def run(
        self,
        *,
        required_variables: tuple[str, ...],
        effective_year: int,
        active_source_by_variable: dict[str, str],
        as_of: datetime | None = None,
    ) -> ProjectionAssumptionPreflightReport:
        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        unique_required = tuple(dict.fromkeys(required_variables))
        checks = tuple(
            self._check_variable(
                variable_key=variable_key,
                effective_year=effective_year,
                active_source_id=active_source_by_variable.get(
                    variable_key,
                    "configured_yaml_default",
                ),
                as_of=as_of,
            )
            for variable_key in unique_required
        )
        return ProjectionAssumptionPreflightReport(
            status=(
                "partial"
                if any(check.status is PreflightStatus.FAILED for check in checks)
                else "complete"
            ),
            effective_year=effective_year,
            required_variables=unique_required,
            checks=checks,
        )

    def _check_variable(
        self,
        *,
        variable_key: str,
        effective_year: int,
        active_source_id: str,
        as_of: datetime,
    ) -> ProjectionAssumptionPreflightCheck:
        policy = self.source_policy.get(variable_key)
        if (
            policy is None
            or policy.source_class is not SourceClass.PUBLIC_AUTHORITATIVE
        ):
            return ProjectionAssumptionPreflightCheck(
                variable_key=variable_key,
                effective_year=effective_year,
                status=PreflightStatus.INELIGIBLE,
                error_code="variable_not_public_authoritative",
            )

        try:
            outcome = self.refresh_service.refresh(
                variable_key=variable_key,
                effective_year=effective_year,
                as_of=as_of,
                existing_source_id=active_source_id,
            )
            decision = outcome.decision
            candidate_id = (
                outcome.candidate_batch.artifacts[0].artifact_id
                if outcome.candidate_batch is not None
                else (
                    decision.current_artifact_id
                    if decision.reason
                    in {
                        RefreshReason.CURRENT_CANDIDATE,
                        RefreshReason.REJECTED_OR_SUPERSEDED,
                    }
                    else None
                )
            )
            approved = self.repository.latest_approved(
                variable_key,
                effective_year=effective_year,
            )
            status = self._status_for_outcome(
                reason=decision.reason,
                candidate_artifact_id=candidate_id,
                approved_artifact_id=(
                    approved.artifact_id if approved is not None else None
                ),
            )
            return ProjectionAssumptionPreflightCheck(
                variable_key=variable_key,
                effective_year=effective_year,
                provider_id=decision.provider_id,
                status=status,
                refresh_reason=decision.reason,
                approved_artifact_id=(
                    approved.artifact_id if approved is not None else None
                ),
                candidate_artifact_id=candidate_id,
            )
        except ProviderAdapterError as exc:
            return ProjectionAssumptionPreflightCheck(
                variable_key=variable_key,
                effective_year=effective_year,
                provider_id=exc.provider_id,
                status=PreflightStatus.FAILED,
                error_code=exc.code.value,
            )
        except AssumptionGovernanceError as exc:
            return ProjectionAssumptionPreflightCheck(
                variable_key=variable_key,
                effective_year=effective_year,
                status=PreflightStatus.FAILED,
                error_code=exc.code.value,
            )
        except Exception:
            return ProjectionAssumptionPreflightCheck(
                variable_key=variable_key,
                effective_year=effective_year,
                status=PreflightStatus.FAILED,
                error_code="preflight_failed",
            )

    def _status_for_outcome(
        self,
        *,
        reason: RefreshReason,
        candidate_artifact_id: str | None,
        approved_artifact_id: str | None,
    ) -> PreflightStatus:
        if reason is RefreshReason.HIGHER_PRECEDENCE_VALUE:
            return PreflightStatus.EXISTING_SOURCE
        if reason is RefreshReason.CURRENT_APPROVED_VALUE:
            return PreflightStatus.CURRENT_APPROVED
        if reason is RefreshReason.CURRENT_CANDIDATE:
            return PreflightStatus.PENDING_REVIEW
        if reason is RefreshReason.REJECTED_OR_SUPERSEDED:
            return PreflightStatus.REJECTED_OR_SUPERSEDED
        if reason is RefreshReason.VARIABLE_NOT_SUPPORTED:
            return PreflightStatus.UNSUPPORTED
        if candidate_artifact_id is not None:
            history = self.repository.decision_history(candidate_artifact_id)
            if history:
                if (
                    history[-1].decision is GovernanceDecision.APPROVE
                    and approved_artifact_id is not None
                ):
                    return PreflightStatus.APPROVED_REVERIFIED
                return PreflightStatus.REJECTED_OR_SUPERSEDED
            return PreflightStatus.CANDIDATE_CREATED
        return PreflightStatus.UNSUPPORTED


__all__ = [
    "PreflightStatus",
    "ProjectionAssumptionPreflightCheck",
    "ProjectionAssumptionPreflightReport",
    "ProjectionAssumptionPreflightService",
]
