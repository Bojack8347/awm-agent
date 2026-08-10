"""Observe-only assumption integration at the AWM cash-flow boundary."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from advisor.assumptions.resolver import (
    AssumptionIntegrationMode,
    ShadowAssumptionResolver,
    assumption_integration_mode,
    build_shadow_assumption_report,
)


_SOCIAL_SECURITY_TAXABLE_MAXIMUM = "social_security_taxable_maximum"
_FINANCIAL_DEFAULTS_PATH = (
    Path(__file__).resolve().parents[2]
    / "advisor"
    / "quant_models"
    / "cashflow_model"
    / "config"
    / "financial_defaults.yaml"
)


def build_cashflow_assumption_shadow(
    *,
    cashflow_payload: dict[str, Any],
    repository: Any,
    mode: AssumptionIntegrationMode | str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """Compare one exact engine default only when this run needs it.

    The current AWM bridge applies the configured Social Security taxable wage
    maximum while computing payroll tax on earned income. Other public
    variables are intentionally out of this first integration slice.
    """

    selected_mode = AssumptionIntegrationMode(
        mode or assumption_integration_mode()
    )
    if selected_mode is AssumptionIntegrationMode.OFF:
        return None

    required_variables = cashflow_required_public_variables(cashflow_payload)
    active_values: dict[str, Any] = {}
    source_by_variable: dict[str, str] = {}
    if _SOCIAL_SECURITY_TAXABLE_MAXIMUM in required_variables:
        active_values[_SOCIAL_SECURITY_TAXABLE_MAXIMUM] = (
            _configured_social_security_taxable_maximum()
        )
        source_by_variable[_SOCIAL_SECURITY_TAXABLE_MAXIMUM] = (
            "life_model.config.tax.fica.social_security_max_income"
        )

    effective_year = cashflow_effective_year(cashflow_payload)
    report = build_shadow_assumption_report(
        resolver=ShadowAssumptionResolver(repository=repository),
        effective_year=effective_year,
        active_values=active_values,
        source_by_variable=source_by_variable,
        required_variables=required_variables,
        mode=selected_mode,
        as_of=as_of,
    )
    return {
        **report,
        "status": "available",
        "scope": "cashflow_projection",
        "integration_slice": "social_security_taxable_maximum",
    }


def unavailable_cashflow_assumption_shadow(
    *,
    cashflow_payload: dict[str, Any],
    error_code: str = "shadow_resolution_failed",
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-blocking audit metadata when shadow comparison fails."""

    report = {
        "schema_version": "awm.assumption_shadow_report.v1",
        "mode": AssumptionIntegrationMode.SHADOW.value,
        "status": "unavailable",
        "scope": "cashflow_projection",
        "integration_slice": "social_security_taxable_maximum",
        "effective_year": cashflow_effective_year(cashflow_payload),
        "required_variables": list(
            cashflow_required_public_variables(cashflow_payload)
        ),
        "evaluated_variables": [],
        "model_inputs_changed": False,
        "resolutions": [],
        "error_code": error_code,
    }
    if preflight is not None:
        report["preflight"] = preflight
    return report


def cashflow_required_public_variables(
    cashflow_payload: dict[str, Any],
) -> tuple[str, ...]:
    income = cashflow_payload.get("income")
    salary = income.get("salary") if isinstance(income, dict) else None
    try:
        has_earned_income = (
            not isinstance(salary, bool)
            and math.isfinite(float(salary))
            and float(salary) > 0
        )
    except (TypeError, ValueError):
        has_earned_income = False
    return (
        (_SOCIAL_SECURITY_TAXABLE_MAXIMUM,)
        if has_earned_income
        else ()
    )


def _configured_social_security_taxable_maximum() -> float:
    with _FINANCIAL_DEFAULTS_PATH.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    value = (
        ((config or {}).get("tax") or {}).get("fica") or {}
    ).get("social_security_max_income")
    if isinstance(value, bool):
        raise ValueError("configured Social Security taxable maximum is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "configured Social Security taxable maximum is invalid"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(
            "configured Social Security taxable maximum is invalid"
        )
    return number


def cashflow_effective_year(cashflow_payload: dict[str, Any]) -> int:
    value = cashflow_payload.get("start_year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        year = datetime.now(timezone.utc).year
    return year if 2000 <= year <= 2200 else datetime.now(timezone.utc).year


__all__ = [
    "build_cashflow_assumption_shadow",
    "cashflow_effective_year",
    "cashflow_required_public_variables",
    "unavailable_cashflow_assumption_shadow",
]
