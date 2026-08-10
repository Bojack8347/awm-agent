"""Deterministic, candidate-only adapters for recurring public variables."""

from advisor.assumptions.providers.base import (
    AuthoritativeProviderAdapter,
    GovernmentSnapshotAdapter,
)
from advisor.assumptions.providers.contracts import (
    AuthoritativeSourceSnapshot,
    ProviderAdapterError,
    ProviderCandidateBatch,
    ProviderErrorCode,
    ProviderRefreshDecision,
    ProviderRefreshOutcome,
    ProviderRequest,
    RefreshReason,
)
from advisor.assumptions.providers.government import (
    CMSProviderAdapter,
    IRSProviderAdapter,
    SSAProviderAdapter,
)
from advisor.assumptions.providers.refresh import ProviderRefreshService
from advisor.assumptions.providers.registry import (
    AuthoritativeProviderRegistry,
    build_default_provider_registry,
)
from advisor.assumptions.providers.repository import (
    AssumptionCandidateRepository,
    InMemoryAssumptionCandidateRepository,
)

__all__ = [
    "AssumptionCandidateRepository",
    "AuthoritativeProviderAdapter",
    "AuthoritativeProviderRegistry",
    "AuthoritativeSourceSnapshot",
    "CMSProviderAdapter",
    "GovernmentSnapshotAdapter",
    "IRSProviderAdapter",
    "InMemoryAssumptionCandidateRepository",
    "ProviderAdapterError",
    "ProviderCandidateBatch",
    "ProviderErrorCode",
    "ProviderRefreshDecision",
    "ProviderRefreshOutcome",
    "ProviderRefreshService",
    "ProviderRequest",
    "RefreshReason",
    "SSAProviderAdapter",
    "build_default_provider_registry",
]
