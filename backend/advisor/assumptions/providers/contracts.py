"""Typed contracts for deterministic authoritative-data providers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionStatus,
    PermittedUse,
    SourceClass,
)


class ProviderErrorCode(str, Enum):
    PROVIDER_NOT_FOUND = "provider_not_found"
    VARIABLE_NOT_SUPPORTED = "variable_not_supported"
    SNAPSHOT_NOT_FOUND = "snapshot_not_found"
    SNAPSHOT_TOO_LARGE = "snapshot_too_large"
    SNAPSHOT_INVALID = "snapshot_invalid"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    VALUE_INVALID = "value_invalid"
    ARTIFACT_CONFLICT = "artifact_conflict"


class ProviderAdapterError(RuntimeError):
    """A stable, non-LLM error raised by an authoritative provider adapter."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        provider_id: str | None = None,
        variable_key: str | None = None,
        effective_year: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.variable_key = variable_key
        self.effective_year = effective_year

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "provider_id": self.provider_id,
            "variable_key": self.variable_key,
            "effective_year": self.effective_year,
        }


class ProviderRequest(BaseModel):
    """One bounded request to a deterministic provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_year: int = Field(ge=2000, le=2200)
    variable_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_variables(self) -> "ProviderRequest":
        cleaned = tuple(key.strip() for key in self.variable_keys)
        if any(not key for key in cleaned):
            raise ValueError("variable keys cannot be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("variable keys must be unique")
        object.__setattr__(self, "variable_keys", cleaned)
        return self


class SnapshotSource(BaseModel):
    """Primary-source metadata contained in a reviewed repository snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    published_at: datetime


class SnapshotVariable(BaseModel):
    """One normalized value plus the primary documents supporting it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Any
    unit: str = Field(min_length=1, max_length=80)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class AuthoritativeSourceSnapshot(BaseModel):
    """Strict, versioned input consumed by government-source adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(
        default="awm.authoritative_source_snapshot.v1",
        pattern=r"^awm\.authoritative_source_snapshot\.v1$",
    )
    provider_id: str = Field(min_length=1, max_length=80)
    effective_year: int = Field(ge=2000, le=2200)
    jurisdiction: str = Field(min_length=1, max_length=160)
    snapshot_version: int = Field(ge=1)
    reviewed_by: str = Field(min_length=1, max_length=240)
    reviewed_at: datetime
    sources: dict[str, SnapshotSource] = Field(min_length=1)
    variables: dict[str, SnapshotVariable] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "AuthoritativeSourceSnapshot":
        if self.reviewed_at.tzinfo is None:
            raise ValueError("snapshot review time must be timezone-aware")
        for source_key, source in self.sources.items():
            if not source_key.strip():
                raise ValueError("snapshot source keys cannot be blank")
            if source.published_at.tzinfo is None:
                raise ValueError("snapshot publication times must be timezone-aware")
        for variable_key, variable in self.variables.items():
            if not variable_key.strip():
                raise ValueError("snapshot variable keys cannot be blank")
            missing = set(variable.evidence_refs).difference(self.sources)
            if missing:
                raise ValueError(
                    f"{variable_key} references unknown evidence: {sorted(missing)}"
                )
        return self


class ProviderCandidateBatch(BaseModel):
    """Candidate artifacts created from one immutable source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "awm.provider_candidate_batch.v1"
    provider_id: str = Field(min_length=1, max_length=80)
    effective_year: int = Field(ge=2000, le=2200)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieved_at: datetime
    artifacts: tuple[AssumptionArtifact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidates(self) -> "ProviderCandidateBatch":
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        variables: set[str] = set()
        for artifact in self.artifacts:
            if artifact.variable_key in variables:
                raise ValueError("candidate batch contains duplicate variables")
            variables.add(artifact.variable_key)
            if artifact.status is not AssumptionStatus.CANDIDATE:
                raise ValueError("provider adapters may only create candidates")
            if artifact.source_class is not SourceClass.PUBLIC_AUTHORITATIVE:
                raise ValueError(
                    "government provider candidates must be public authoritative"
                )
            if artifact.permitted_uses != (PermittedUse.REPORTING,):
                raise ValueError(
                    "provider candidates must remain reporting-only until approved"
                )
        return self


class RefreshReason(str, Enum):
    HIGHER_PRECEDENCE_VALUE = "higher_precedence_value"
    CURRENT_CANDIDATE = "current_candidate"
    CURRENT_APPROVED_VALUE = "current_approved_value"
    MISSING = "missing"
    EFFECTIVE_YEAR_MISMATCH = "effective_year_mismatch"
    STALE = "stale"
    REJECTED_OR_SUPERSEDED = "rejected_or_superseded"
    FORCED = "forced"
    VARIABLE_NOT_SUPPORTED = "variable_not_supported"


class ProviderRefreshDecision(BaseModel):
    """Deterministic explanation of whether a provider should run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str
    effective_year: int
    should_refresh: bool
    reason: RefreshReason
    provider_id: str | None = None
    existing_source_id: str | None = None
    current_artifact_id: str | None = None
    current_value_preserved: bool = True


class ProviderRefreshOutcome(BaseModel):
    """Refresh decision and optional candidate batch; never an activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ProviderRefreshDecision
    candidate_batch: ProviderCandidateBatch | None = None
    activated: bool = False

    @model_validator(mode="after")
    def validate_non_activation(self) -> "ProviderRefreshOutcome":
        if self.activated:
            raise ValueError("provider refresh cannot activate assumptions")
        if self.decision.should_refresh != (self.candidate_batch is not None):
            raise ValueError("refresh decision and candidate batch disagree")
        return self
