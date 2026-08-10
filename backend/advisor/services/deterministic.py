"""Deterministic service adapter contracts for AWM agent v2.

These adapters sit behind guarded service workflows such as KYC, execution,
settlement, and holdings ingestion. They deliberately do not call LLMs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Dict, Mapping, Protocol


@dataclass(frozen=True)
class DeterministicServiceRequest:
    service_name: str
    client_id: str
    session_id: str
    user_message: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeterministicServiceAdapterResult:
    service_name: str
    adapter_name: str
    adapter_status: str
    next_status: str
    external_reference: str | None = None
    payload: Dict[str, Any] = field(default_factory=dict)


class DeterministicServiceAdapter(Protocol):
    adapter_name: str

    def initiate(self, request: DeterministicServiceRequest) -> DeterministicServiceAdapterResult:
        """Initiate or enqueue the deterministic service action."""


class PendingExternalServiceAdapter:
    """Default adapter that records a handoff without performing external work."""

    def __init__(self, *, service_name: str, adapter_name: str, next_status: str) -> None:
        self.service_name = service_name
        self.adapter_name = adapter_name
        self.next_status = next_status

    def initiate(self, request: DeterministicServiceRequest) -> DeterministicServiceAdapterResult:
        return DeterministicServiceAdapterResult(
            service_name=request.service_name,
            adapter_name=self.adapter_name,
            adapter_status="pending_external_adapter",
            next_status=self.next_status,
            payload={
                "external_adapter_required": True,
                "message": "No production adapter is configured for this deterministic service.",
            },
        )


class DeterministicServiceAdapterRegistry:
    """Lookup table for deterministic service adapters."""

    def __init__(self, adapters: Mapping[str, DeterministicServiceAdapter]):
        self._adapters = dict(adapters)

    def get(self, service_name: str) -> DeterministicServiceAdapter:
        return self._adapters[service_name]

    def initiate(self, request: DeterministicServiceRequest) -> DeterministicServiceAdapterResult:
        return self.get(request.service_name).initiate(request)


def build_pending_service_adapter_registry(
    service_contracts: Mapping[str, Mapping[str, Any]],
) -> DeterministicServiceAdapterRegistry:
    return DeterministicServiceAdapterRegistry(
        {
            service_name: PendingExternalServiceAdapter(
                service_name=service_name,
                adapter_name=str(contract.get("adapter") or f"{service_name}_adapter_pending"),
                next_status=str(contract.get("next_status") or "adapter_pending"),
            )
            for service_name, contract in service_contracts.items()
        }
    )


def build_production_service_adapter_registry(
    service_contracts: Mapping[str, Mapping[str, Any]],
    *,
    env_getter: Any = os.getenv,
) -> DeterministicServiceAdapterRegistry:
    """Build regulated-service adapters for production wiring.

    `pending` is the only bundled mode today. It records a deterministic,
    auditable handoff and performs no external action. Any requested external
    mode must fail fast until vendor-specific adapters are implemented and
    explicitly wired.
    """

    mode = str(env_getter("AWM_V2_DETERMINISTIC_SERVICE_ADAPTERS", "pending") or "pending").strip().lower()
    if mode in {"", "pending", "off", "false", "0"}:
        return build_pending_service_adapter_registry(service_contracts)
    raise RuntimeError(
        "AWM_V2_DETERMINISTIC_SERVICE_ADAPTERS currently supports only "
        "'pending'. Vendor-specific regulated service adapters must be wired "
        "explicitly before enabling external mode."
    )
