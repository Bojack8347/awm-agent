"""Cash-flow declaration wrapper for generic assumption preflight."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from advisor.assumptions.preflight import (
    ProjectionAssumptionPreflightReport,
    ProjectionAssumptionPreflightService,
)
from advisor.assumptions.providers.registry import (
    build_default_provider_registry,
)
from advisor.assumptions.resolution import (
    VariableResolutionFacade,
    VariableResolutionFacadeReport,
)
from api.services.assumption_shadow import (
    cashflow_effective_year,
    cashflow_required_public_variables,
)
from api.services.assumption_research import (
    build_runtime_research_specialist,
)

_AUTO_RESEARCH_SPECIALIST = object()


def run_cashflow_assumption_preflight(
    *,
    cashflow_payload: dict[str, Any],
    repository: Any,
    as_of: datetime | None = None,
) -> ProjectionAssumptionPreflightReport:
    """Resolve required variables while preserving the existing return type."""

    return run_cashflow_assumption_resolution(
        cashflow_payload=cashflow_payload,
        repository=repository,
        as_of=as_of,
    ).preflight


def run_cashflow_assumption_resolution(
    *,
    cashflow_payload: dict[str, Any],
    repository: Any,
    as_of: datetime | None = None,
    research_specialist: Any = _AUTO_RESEARCH_SPECIALIST,
) -> VariableResolutionFacadeReport:
    """Run the unified source chain for public cash-flow variables."""

    required_variables = cashflow_required_public_variables(cashflow_payload)
    specialist = (
        build_runtime_research_specialist(repository=repository)
        if research_specialist is _AUTO_RESEARCH_SPECIALIST
        else research_specialist
    )
    return VariableResolutionFacade(
        deterministic_preflight=ProjectionAssumptionPreflightService(
            providers=build_default_provider_registry(),
            repository=repository,
        ),
        research_specialist=specialist,
    ).resolve(
        required_variables=required_variables,
        effective_year=cashflow_effective_year(cashflow_payload),
        active_source_by_variable={
            variable_key: (
                "life_model.config.tax.fica.social_security_max_income"
            )
            for variable_key in required_variables
        },
        as_of=as_of,
    )


__all__ = [
    "run_cashflow_assumption_preflight",
    "run_cashflow_assumption_resolution",
]
