"""Precedence- and freshness-aware provider refresh orchestration."""

from __future__ import annotations

from datetime import datetime, timezone

from advisor.assumptions.contracts import AssumptionArtifact, AssumptionStatus
from advisor.assumptions.providers.contracts import (
    ProviderRefreshDecision,
    ProviderRefreshOutcome,
    ProviderRequest,
    RefreshReason,
)
from advisor.assumptions.providers.registry import AuthoritativeProviderRegistry
from advisor.assumptions.providers.repository import AssumptionCandidateRepository
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


HIGHER_PRECEDENCE_SOURCES = frozenset(
    {
        "current_user_input",
        "confirmed_client_fact",
        "structured_client_fact",
        "deterministic_derivation",
    }
)


class ProviderRefreshService:
    """Collect candidates only when policy says a refresh is warranted."""

    def __init__(
        self,
        *,
        providers: AuthoritativeProviderRegistry,
        repository: AssumptionCandidateRepository,
        source_policy: VariableSourceRegistry | None = None,
    ) -> None:
        self.providers = providers
        self.repository = repository
        self.source_policy = source_policy or load_variable_source_registry()

    def decide(
        self,
        *,
        variable_key: str,
        effective_year: int,
        as_of: datetime | None = None,
        existing_source_id: str | None = None,
        force_refresh: bool = False,
    ) -> ProviderRefreshDecision:
        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")

        adapter = self.providers.provider_for_variable(variable_key)
        governed_candidate = self._governed_candidate_state(
            variable_key=variable_key,
            effective_year=effective_year,
        )
        current_for_year = self.repository.latest(
            variable_key,
            effective_year=effective_year,
        )
        current = current_for_year or self.repository.latest(variable_key)
        normalized_source = (
            self.source_policy.normalize_source(existing_source_id)
            if existing_source_id
            else None
        )
        common = {
            "variable_key": variable_key,
            "effective_year": effective_year,
            "provider_id": adapter.provider_id if adapter else None,
            "existing_source_id": existing_source_id,
            "current_artifact_id": current.artifact_id if current else None,
            "current_value_preserved": True,
        }

        if normalized_source in HIGHER_PRECEDENCE_SOURCES:
            return ProviderRefreshDecision(
                should_refresh=False,
                reason=RefreshReason.HIGHER_PRECEDENCE_VALUE,
                **common,
            )
        if adapter is None:
            return ProviderRefreshDecision(
                should_refresh=False,
                reason=RefreshReason.VARIABLE_NOT_SUPPORTED,
                **common,
            )
        if governed_candidate is not None and not force_refresh:
            candidate_status, candidate_artifact_id = governed_candidate
            governed_common = {
                **common,
                "current_artifact_id": candidate_artifact_id,
            }
            return ProviderRefreshDecision(
                should_refresh=False,
                reason=(
                    RefreshReason.CURRENT_CANDIDATE
                    if candidate_status == "pending"
                    else RefreshReason.REJECTED_OR_SUPERSEDED
                ),
                **governed_common,
            )
        if current is None:
            return ProviderRefreshDecision(
                should_refresh=True,
                reason=RefreshReason.FORCED if force_refresh else RefreshReason.MISSING,
                **common,
            )
        if current_for_year is None or (
            current.effective_from is None
            or current.effective_from.year != effective_year
        ):
            return ProviderRefreshDecision(
                should_refresh=True,
                reason=RefreshReason.EFFECTIVE_YEAR_MISMATCH,
                **common,
            )
        if force_refresh:
            return ProviderRefreshDecision(
                should_refresh=True,
                reason=RefreshReason.FORCED,
                **common,
            )
        if current.status in {
            AssumptionStatus.REJECTED,
            AssumptionStatus.SUPERSEDED,
        }:
            return ProviderRefreshDecision(
                should_refresh=False,
                reason=RefreshReason.REJECTED_OR_SUPERSEDED,
                **common,
            )

        policy = self.source_policy.require(variable_key)
        latest_retrieval = self._latest_retrieval(current)
        if (
            policy.maximum_age_days is not None
            and (as_of - latest_retrieval).days > policy.maximum_age_days
        ):
            return ProviderRefreshDecision(
                should_refresh=True,
                reason=RefreshReason.STALE,
                **common,
            )
        return ProviderRefreshDecision(
            should_refresh=False,
            reason=(
                RefreshReason.CURRENT_APPROVED_VALUE
                if current.status is AssumptionStatus.APPROVED
                else RefreshReason.CURRENT_CANDIDATE
            ),
            **common,
        )

    def refresh(
        self,
        *,
        variable_key: str,
        effective_year: int,
        as_of: datetime | None = None,
        existing_source_id: str | None = None,
        force_refresh: bool = False,
    ) -> ProviderRefreshOutcome:
        as_of = as_of or datetime.now(timezone.utc)
        decision = self.decide(
            variable_key=variable_key,
            effective_year=effective_year,
            as_of=as_of,
            existing_source_id=existing_source_id,
            force_refresh=force_refresh,
        )
        if not decision.should_refresh:
            return ProviderRefreshOutcome(decision=decision)

        adapter = self.providers.provider_for_variable(variable_key)
        if adapter is None:  # Defensive; decide() already covers this.
            return ProviderRefreshOutcome(decision=decision)
        batch = adapter.collect_candidates(
            ProviderRequest(
                effective_year=effective_year,
                variable_keys=(variable_key,),
            ),
            retrieved_at=as_of,
        )
        self.repository.save_batch(batch)
        return ProviderRefreshOutcome(
            decision=decision,
            candidate_batch=batch,
        )

    @staticmethod
    def _latest_retrieval(artifact: AssumptionArtifact) -> datetime:
        timestamps = [
            evidence.retrieved_at for evidence in artifact.evidence
        ] or [artifact.created_at]
        latest = max(timestamps)
        if latest.tzinfo is None:
            raise ValueError("artifact retrieval times must be timezone-aware")
        return latest

    def _governed_candidate_state(
        self,
        *,
        variable_key: str,
        effective_year: int,
    ) -> tuple[str, str] | None:
        """Prefer unresolved governance state over an older active approval."""

        listing = getattr(self.repository, "list_candidates", None)
        if not callable(listing):
            return None
        for governance_status in ("pending", "rejected"):
            reviews = listing(
                variable_key=variable_key,
                effective_year=effective_year,
                governance_status=governance_status,
                limit=1,
            )
            if reviews:
                return (
                    governance_status,
                    reviews[0].candidate.artifact_id,
                )
        return None
