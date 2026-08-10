"""Registry for deterministic provider adapters."""

from __future__ import annotations

from advisor.assumptions.providers.base import AuthoritativeProviderAdapter
from advisor.assumptions.providers.contracts import (
    ProviderAdapterError,
    ProviderErrorCode,
)
from advisor.assumptions.providers.government import (
    CMSProviderAdapter,
    IRSProviderAdapter,
    SSAProviderAdapter,
)


class AuthoritativeProviderRegistry:
    """Immutable provider and variable lookup with duplicate protection."""

    def __init__(
        self, adapters: tuple[AuthoritativeProviderAdapter, ...]
    ) -> None:
        by_provider: dict[str, AuthoritativeProviderAdapter] = {}
        by_variable: dict[str, AuthoritativeProviderAdapter] = {}
        for adapter in adapters:
            if adapter.provider_id in by_provider:
                raise ValueError(f"duplicate provider adapter: {adapter.provider_id}")
            by_provider[adapter.provider_id] = adapter
            for variable_key in adapter.supported_variables:
                if variable_key in by_variable:
                    raise ValueError(
                        f"multiple providers registered for {variable_key}"
                    )
                by_variable[variable_key] = adapter
        self._by_provider = by_provider
        self._by_variable = by_variable

    def provider(self, provider_id: str) -> AuthoritativeProviderAdapter:
        try:
            return self._by_provider[provider_id]
        except KeyError as exc:
            raise ProviderAdapterError(
                ProviderErrorCode.PROVIDER_NOT_FOUND,
                f"authoritative provider not registered: {provider_id}",
                provider_id=provider_id,
            ) from exc

    def provider_for_variable(
        self, variable_key: str
    ) -> AuthoritativeProviderAdapter | None:
        return self._by_variable.get(variable_key)

    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_provider))

    def variable_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_variable))


def build_default_provider_registry() -> AuthoritativeProviderRegistry:
    return AuthoritativeProviderRegistry(
        (
            IRSProviderAdapter(),
            SSAProviderAdapter(),
            CMSProviderAdapter(),
        )
    )
