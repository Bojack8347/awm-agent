"""Service adapter contracts for agent v2."""

from advisor.services.deterministic import (
    DeterministicServiceAdapterRegistry,
    DeterministicServiceAdapterResult,
    DeterministicServiceRequest,
    PendingExternalServiceAdapter,
    build_pending_service_adapter_registry,
    build_production_service_adapter_registry,
)

__all__ = [
    "DeterministicServiceAdapterRegistry",
    "DeterministicServiceAdapterResult",
    "DeterministicServiceRequest",
    "PendingExternalServiceAdapter",
    "build_pending_service_adapter_registry",
    "build_production_service_adapter_registry",
]
