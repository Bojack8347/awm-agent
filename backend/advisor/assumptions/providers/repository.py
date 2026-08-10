"""Storage boundary for provider candidates.

The in-memory implementation is intentionally process-local. Production
persistence can implement the same protocol without changing provider code.
"""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from advisor.assumptions.contracts import AssumptionArtifact, AssumptionStatus
from advisor.assumptions.providers.contracts import (
    ProviderAdapterError,
    ProviderCandidateBatch,
    ProviderErrorCode,
)


class AssumptionCandidateRepository(Protocol):
    def latest(
        self,
        variable_key: str,
        *,
        effective_year: int | None = None,
    ) -> AssumptionArtifact | None: ...

    def save_batch(self, batch: ProviderCandidateBatch) -> None: ...


def _stable_content_payload(artifact: AssumptionArtifact) -> dict:
    payload = artifact.model_dump(mode="json")
    for key in (
        "created_at",
        "status",
        "permitted_uses",
        "approved_by",
        "approved_at",
    ):
        payload.pop(key, None)
    for evidence in payload.get("evidence", []):
        evidence.pop("retrieved_at", None)
    return payload


class InMemoryAssumptionCandidateRepository:
    """Thread-safe candidate store used until durable storage is connected."""

    def __init__(
        self, artifacts: tuple[AssumptionArtifact, ...] = ()
    ) -> None:
        self._lock = RLock()
        self._by_id: dict[str, AssumptionArtifact] = {}
        self._by_variable: dict[str, list[str]] = {}
        for artifact in artifacts:
            self._save(artifact)

    def latest(
        self,
        variable_key: str,
        *,
        effective_year: int | None = None,
    ) -> AssumptionArtifact | None:
        with self._lock:
            artifact_ids = self._by_variable.get(variable_key, [])
            if not artifact_ids:
                return None
            artifacts = [self._by_id[artifact_id] for artifact_id in artifact_ids]
            if effective_year is not None:
                artifacts = [
                    artifact
                    for artifact in artifacts
                    if artifact.effective_from is not None
                    and artifact.effective_from.year == effective_year
                ]
            if not artifacts:
                return None
            status_priority = {
                AssumptionStatus.APPROVED: 3,
                AssumptionStatus.CANDIDATE: 2,
                AssumptionStatus.REJECTED: 1,
                AssumptionStatus.SUPERSEDED: 0,
            }
            return max(
                artifacts,
                key=lambda artifact: (
                    status_priority[artifact.status],
                    artifact.created_at,
                ),
            )

    def save_batch(self, batch: ProviderCandidateBatch) -> None:
        with self._lock:
            for artifact in batch.artifacts:
                existing = self._by_id.get(artifact.artifact_id)
                if existing is not None:
                    if _stable_content_payload(existing) != _stable_content_payload(
                        artifact
                    ):
                        raise ProviderAdapterError(
                            ProviderErrorCode.ARTIFACT_CONFLICT,
                            f"artifact id collision: {artifact.artifact_id}",
                            provider_id=batch.provider_id,
                            variable_key=artifact.variable_key,
                            effective_year=batch.effective_year,
                        )
                    if existing.status is AssumptionStatus.CANDIDATE:
                        # Same reviewed candidate collected again. Refresh its
                        # retrieval metadata without creating a duplicate.
                        self._by_id[artifact.artifact_id] = artifact
                    # Never replace an approved/rejected server decision with
                    # an incoming candidate carrying the same content.
                    continue
                self._save(artifact)

    def all(self) -> tuple[AssumptionArtifact, ...]:
        with self._lock:
            return tuple(self._by_id.values())

    def _save(self, artifact: AssumptionArtifact) -> None:
        self._by_id[artifact.artifact_id] = artifact
        self._by_variable.setdefault(artifact.variable_key, []).append(
            artifact.artifact_id
        )
