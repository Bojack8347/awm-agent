"""Unified, additive façade for resolving required projection variables."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from advisor.assumptions.contracts import SourceClass
from advisor.assumptions.preflight import (
    PreflightStatus,
    ProjectionAssumptionPreflightCheck,
    ProjectionAssumptionPreflightReport,
    ProjectionAssumptionPreflightService,
)
from advisor.assumptions.research import (
    ResearchRequest,
    ResearchSpecialist,
    ResearchSpecialistError,
)
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


class VariableResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    NEEDS_CLIENT = "needs_client"
    PENDING_APPROVAL = "pending_approval"
    UNAVAILABLE = "unavailable"


class VariableResolutionStage(str, Enum):
    EXISTING_SOURCE = "existing_source"
    APPROVED_REGISTRY = "approved_registry"
    DETERMINISTIC_PROVIDER = "deterministic_provider"
    RESEARCH_SPECIALIST = "research_specialist"
    TERMINAL = "terminal"


class VariableResolutionResult(BaseModel):
    """Small stable result consumed by projection-specific wrappers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str
    effective_year: int
    status: VariableResolutionStatus
    stage: VariableResolutionStage
    reason_code: str
    approved_artifact_id: str | None = None
    candidate_artifact_id: str | None = None
    research_attempted: bool = False
    terminal: bool = True


class VariableResolutionFacadeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.variable_resolution_facade.v1"
    status: Literal["complete", "partial"]
    effective_year: int
    required_variables: tuple[str, ...]
    resolutions: tuple[VariableResolutionResult, ...]
    preflight: ProjectionAssumptionPreflightReport


class VariableResolutionFacade:
    """Apply one source policy and one bounded fallback chain.

    Existing deterministic preflight remains authoritative.  Research is
    considered only after that path is unsupported or fails, and only for
    public-authoritative variables whose policy explicitly permits it.
    """

    def __init__(
        self,
        *,
        deterministic_preflight: ProjectionAssumptionPreflightService,
        research_specialist: ResearchSpecialist | None = None,
        source_policy: VariableSourceRegistry | None = None,
    ) -> None:
        self.deterministic_preflight = deterministic_preflight
        self.research_specialist = research_specialist
        self.source_policy = source_policy or load_variable_source_registry()

    def resolve(
        self,
        *,
        required_variables: tuple[str, ...],
        effective_year: int,
        active_source_by_variable: dict[str, str],
        as_of: datetime | None = None,
    ) -> VariableResolutionFacadeReport:
        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        preflight = self.deterministic_preflight.run(
            required_variables=required_variables,
            effective_year=effective_year,
            active_source_by_variable=active_source_by_variable,
            as_of=as_of,
        )
        updated_checks: list[ProjectionAssumptionPreflightCheck] = []
        resolutions: list[VariableResolutionResult] = []
        for check in preflight.checks:
            updated, resolution = self._resolve_check(check, as_of=as_of)
            updated_checks.append(updated)
            resolutions.append(resolution)

        updated_preflight = ProjectionAssumptionPreflightReport(
            status=(
                "partial"
                if any(
                    check.status is PreflightStatus.FAILED
                    for check in updated_checks
                )
                else "complete"
            ),
            effective_year=effective_year,
            required_variables=preflight.required_variables,
            checks=tuple(updated_checks),
            research_attempted=any(
                check.research_attempted for check in updated_checks
            ),
        )
        return VariableResolutionFacadeReport(
            status=updated_preflight.status,
            effective_year=effective_year,
            required_variables=preflight.required_variables,
            resolutions=tuple(resolutions),
            preflight=updated_preflight,
        )

    def _resolve_check(
        self,
        check: ProjectionAssumptionPreflightCheck,
        *,
        as_of: datetime,
    ) -> tuple[
        ProjectionAssumptionPreflightCheck,
        VariableResolutionResult,
    ]:
        common = {
            "variable_key": check.variable_key,
            "effective_year": check.effective_year,
            "approved_artifact_id": check.approved_artifact_id,
            "candidate_artifact_id": check.candidate_artifact_id,
        }
        if check.status is PreflightStatus.EXISTING_SOURCE:
            return check, VariableResolutionResult(
                status=VariableResolutionStatus.RESOLVED,
                stage=VariableResolutionStage.EXISTING_SOURCE,
                reason_code=check.status.value,
                **common,
            )
        if check.status in {
            PreflightStatus.CURRENT_APPROVED,
            PreflightStatus.APPROVED_REVERIFIED,
        }:
            return check, VariableResolutionResult(
                status=VariableResolutionStatus.RESOLVED,
                stage=VariableResolutionStage.APPROVED_REGISTRY,
                reason_code=check.status.value,
                **common,
            )
        if check.status in {
            PreflightStatus.CANDIDATE_CREATED,
            PreflightStatus.PENDING_REVIEW,
        }:
            return check, VariableResolutionResult(
                status=VariableResolutionStatus.PENDING_APPROVAL,
                stage=VariableResolutionStage.DETERMINISTIC_PROVIDER,
                reason_code=check.status.value,
                **common,
            )
        if check.status is PreflightStatus.REJECTED_OR_SUPERSEDED:
            return check, VariableResolutionResult(
                status=VariableResolutionStatus.UNAVAILABLE,
                stage=VariableResolutionStage.TERMINAL,
                reason_code=check.status.value,
                **common,
            )

        policy = self.source_policy.get(check.variable_key)
        if (
            policy is None
            or policy.source_class is not SourceClass.PUBLIC_AUTHORITATIVE
        ):
            needs_client = (
                policy is not None
                and policy.source_class is SourceClass.CLIENT_SPECIFIC
            )
            return check, VariableResolutionResult(
                status=(
                    VariableResolutionStatus.NEEDS_CLIENT
                    if needs_client
                    else VariableResolutionStatus.UNAVAILABLE
                ),
                stage=VariableResolutionStage.TERMINAL,
                reason_code=(
                    "client_value_required"
                    if needs_client
                    else check.error_code or check.status.value
                ),
                **common,
            )

        researchable_provider_errors = {
            "provider_not_found",
            "variable_not_supported",
            "snapshot_not_found",
            "snapshot_too_large",
            "snapshot_invalid",
            "source_not_allowed",
            "value_invalid",
        }
        research_eligible = (
            policy.online_research_allowed
            and policy.approval_required
            and (
                check.status is PreflightStatus.UNSUPPORTED
                or (
                    check.status is PreflightStatus.FAILED
                    and check.error_code in researchable_provider_errors
                )
            )
        )
        if not research_eligible or self.research_specialist is None:
            return check, VariableResolutionResult(
                status=VariableResolutionStatus.UNAVAILABLE,
                stage=VariableResolutionStage.TERMINAL,
                reason_code=(
                    "research_disabled"
                    if research_eligible
                    else check.error_code or check.status.value
                ),
                **common,
            )

        try:
            batch = self.research_specialist.collect_candidate(
                ResearchRequest(
                    variable_key=check.variable_key,
                    effective_year=check.effective_year,
                ),
                retrieved_at=as_of,
            )
            candidate_id = batch.artifacts[0].artifact_id
            updated = check.model_copy(
                update={
                    "provider_id": batch.provider_id,
                    "status": PreflightStatus.RESEARCH_CANDIDATE_CREATED,
                    "candidate_artifact_id": candidate_id,
                    "research_attempted": True,
                    "error_code": None,
                }
            )
            return updated, VariableResolutionResult(
                status=VariableResolutionStatus.PENDING_APPROVAL,
                stage=VariableResolutionStage.RESEARCH_SPECIALIST,
                reason_code=PreflightStatus.RESEARCH_CANDIDATE_CREATED.value,
                approved_artifact_id=check.approved_artifact_id,
                candidate_artifact_id=candidate_id,
                research_attempted=True,
                variable_key=check.variable_key,
                effective_year=check.effective_year,
            )
        except ResearchSpecialistError as exc:
            updated = check.model_copy(
                update={
                    "status": PreflightStatus.FAILED,
                    "research_attempted": exc.attempted,
                    "error_code": exc.code.value,
                }
            )
            return updated, VariableResolutionResult(
                status=VariableResolutionStatus.UNAVAILABLE,
                stage=VariableResolutionStage.TERMINAL,
                reason_code=exc.code.value,
                research_attempted=exc.attempted,
                **common,
            )


__all__ = [
    "VariableResolutionFacade",
    "VariableResolutionFacadeReport",
    "VariableResolutionResult",
    "VariableResolutionStage",
    "VariableResolutionStatus",
]
