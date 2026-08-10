"""Pure arithmetic over typed values from one stored cash-flow analysis."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Mapping, Optional


_OPERATIONS = {
    "difference",
    "absolute_difference",
    "ratio",
    "percentage_change",
    "probability_complement",
    "future_value",
    "present_value",
    "compound_annual_growth_rate",
}
_PATH_TOKEN_RE = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]")
_METRIC_KEY_ALIASES = {
    "ending_balance_percentiles": "terminal_value_percentiles",
    "terminal_balance": "terminal_value_percentiles",
    "terminal_balance_percentiles": "terminal_value_percentiles",
    "terminal_net_worth": "terminal_value_percentiles",
    "terminal_net_worth_percentiles": "terminal_value_percentiles",
}


def calculate_cashflow_metrics(
    *,
    analysis_id: str,
    recommendation_evidence: Mapping[str, Any],
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve source claims server-side and calculate a reconciled result."""

    operation = str(arguments.get("operation") or "").strip()
    if operation not in _OPERATIONS:
        return {"success": False, "error": "cashflow_metric_operation_invalid"}
    try:
        primary = _resolve_metric(recommendation_evidence, arguments.get("primary"))
        secondary_ref = arguments.get("secondary")
        secondary = (
            _resolve_metric(recommendation_evidence, secondary_ref)
            if secondary_ref is not None
            else None
        )
        annual_rate = _optional_finite(
            arguments.get("annual_rate_decimal"),
            "annual_rate_decimal",
        )
        periods = _optional_finite(arguments.get("periods"), "periods")
        compounds = arguments.get("compounds_per_year")
        if compounds is not None and (
            isinstance(compounds, bool)
            or not isinstance(compounds, int)
            or compounds < 1
            or compounds > 365
        ):
            raise ValueError("compounds_per_year_must_be_an_integer_from_1_to_365")
        value, unit, formula = _calculate(
            operation=operation,
            primary=primary,
            secondary=secondary,
            annual_rate=annual_rate,
            periods=periods,
            compounds_per_year=compounds,
        )
        comparison_context = _comparison_context(
            operation=operation,
            primary=primary,
            secondary=secondary,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if not math.isfinite(value):
        return {"success": False, "error": "cashflow_metric_result_not_finite"}

    normalized_value = _normalized_float(value)
    return {
        "success": True,
        "full_result": {
            "schema_version": "awm.cashflow_metric_calculation.v1",
            "analysis_id": analysis_id,
            "operation": operation,
            "primary": primary,
            "secondary": secondary,
            "parameters": {
                "annual_rate_decimal": annual_rate,
                "periods": periods,
                "compounds_per_year": compounds,
            },
            "result": {"value": normalized_value, "unit": unit},
            "formula": formula,
            "comparison_context": comparison_context,
            "reconciliation": {
                "recomputed_value": normalized_value,
                "difference": 0.0,
            },
            "calculation_policy": (
                "Source values were resolved from typed evidence in one fresh immutable "
                "cash-flow analysis. LifeModel was not rerun, and the arithmetic does "
                "not create a financial recommendation."
            ),
        },
    }


def _resolve_metric(
    evidence: Mapping[str, Any],
    raw_reference: Any,
) -> Dict[str, Any]:
    if not isinstance(raw_reference, Mapping):
        raise ValueError("cashflow_metric_reference_required")
    requested_metric_key = str(raw_reference.get("metric_key") or "").strip()
    metric_key = _METRIC_KEY_ALIASES.get(
        requested_metric_key.lower(),
        requested_metric_key,
    )
    if not metric_key:
        raise ValueError("cashflow_metric_key_required")
    claims = [
        item
        for item in evidence.get("claims") or []
        if isinstance(item, Mapping)
        and str(item.get("metric_key") or "").strip() == metric_key
    ]
    if len(claims) != 1:
        raise ValueError(
            "cashflow_metric_not_found"
            if not claims
            else "cashflow_metric_key_ambiguous"
        )
    claim = claims[0]
    raw_path = raw_reference.get("value_path")
    value_path = str(raw_path).strip() if raw_path is not None else None
    if value_path and metric_key.endswith("_percentiles"):
        value_path = value_path.removeprefix("percentiles.")
    value = claim.get("value")
    if value_path:
        value = _value_at_path(value, value_path)
    numeric = _finite(value, "cashflow_metric_value")
    return {
        "metric_key": metric_key,
        "value_path": value_path,
        "value": numeric,
        "unit": str(claim.get("unit") or "").strip(),
        "claim_id": str(claim.get("claim_id") or metric_key),
        "evidence_ref": str(claim.get("evidence_ref") or ""),
        "source_path": str(claim.get("source_path") or ""),
    }


def _value_at_path(value: Any, path: str) -> Any:
    normalized = path.strip().removeprefix("$.").removeprefix("$")
    if not normalized:
        return value
    matches = list(_PATH_TOKEN_RE.finditer(normalized))
    if not matches or "".join(match.group(0) for match in matches).lstrip(".") != normalized:
        raise ValueError("cashflow_metric_value_path_invalid")
    current = value
    for match in matches:
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, Mapping) or key not in current:
                raise ValueError("cashflow_metric_value_path_not_found")
            current = current[key]
        else:
            if not isinstance(current, list):
                raise ValueError("cashflow_metric_value_path_not_found")
            position = int(index)
            if position >= len(current):
                raise ValueError("cashflow_metric_value_path_not_found")
            current = current[position]
    return current


def _calculate(
    *,
    operation: str,
    primary: Dict[str, Any],
    secondary: Optional[Dict[str, Any]],
    annual_rate: Optional[float],
    periods: Optional[float],
    compounds_per_year: Optional[int],
) -> tuple[float, str, str]:
    left = float(primary["value"])
    left_unit = str(primary["unit"])
    if operation == "probability_complement":
        if not _probability_unit(left_unit) or not 0.0 <= left <= 1.0:
            raise ValueError("probability_complement_requires_probability")
        return 1.0 - left, "probability_0_to_1", "1 - primary_value"

    if operation in {"future_value", "present_value"}:
        if not _currency_unit(left_unit):
            raise ValueError(f"{operation}_requires_currency_metric")
        rate = _required(annual_rate, "annual_rate_decimal")
        years = _positive(_required(periods, "periods"), "periods")
        frequency = compounds_per_year or 1
        periodic_rate = rate / frequency
        factor = (1.0 + periodic_rate) ** (years * frequency)
        if operation == "future_value":
            return (
                left * factor,
                "USD",
                "primary_value * (1 + annual_rate_decimal / compounds_per_year) "
                "** (periods * compounds_per_year)",
            )
        if factor == 0:
            raise ValueError("present_value_discount_factor_is_zero")
        return (
            left / factor,
            "USD",
            "primary_value / (1 + annual_rate_decimal / compounds_per_year) "
            "** (periods * compounds_per_year)",
        )

    if secondary is None:
        raise ValueError("secondary_metric_required_for_operation")
    right = float(secondary["value"])
    right_unit = str(secondary["unit"])
    if left_unit != right_unit:
        raise ValueError("cashflow_metric_units_do_not_match")
    if operation == "difference":
        return right - left, left_unit, "secondary_value - primary_value"
    if operation == "absolute_difference":
        return abs(right - left), left_unit, "abs(secondary_value - primary_value)"
    if operation == "ratio":
        if left == 0:
            raise ValueError("ratio_denominator_must_be_nonzero")
        return right / left, "ratio", "secondary_value / primary_value"
    if operation == "percentage_change":
        if left == 0:
            raise ValueError("percentage_change_base_must_be_nonzero")
        return (
            (right - left) / abs(left),
            "decimal_change",
            "(secondary_value - primary_value) / abs(primary_value)",
        )
    if operation == "compound_annual_growth_rate":
        years = _positive(_required(periods, "periods"), "periods")
        if left <= 0 or right < 0:
            raise ValueError("cagr_requires_positive_start_and_nonnegative_end")
        return (
            (right / left) ** (1.0 / years) - 1.0,
            "annual_decimal",
            "(secondary_value / primary_value) ** (1 / periods) - 1",
        )
    raise ValueError("cashflow_metric_operation_invalid")


def _comparison_context(
    *,
    operation: str,
    primary: Dict[str, Any],
    secondary: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Describe when a multiplicative comparison crosses zero."""

    if operation not in {"ratio", "percentage_change"} or secondary is None:
        return None
    primary_value = float(primary["value"])
    secondary_value = float(secondary["value"])
    signed_difference = _normalized_float(secondary_value - primary_value)
    crosses_zero = primary_value * secondary_value < 0.0
    return {
        "crosses_zero": crosses_zero,
        "sign_relation": (
            "opposite_sides_of_zero"
            if crosses_zero
            else "same_side_or_zero"
        ),
        "signed_difference": signed_difference,
        "absolute_difference": _normalized_float(abs(signed_difference)),
        "difference_unit": str(primary["unit"]),
        "lead_metric": (
            "signed_difference" if crosses_zero else "requested_result"
        ),
        "warning": (
            "The source and comparison values are on opposite sides of zero. "
            "The requested ratio or percentage change is mathematically defined "
            "but is not an intuitive times-larger or percent-higher comparison; "
            "lead with the signed difference."
            if crosses_zero
            else None
        ),
    }


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_must_be_numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field}_must_be_finite")
    return number


def _optional_finite(value: Any, field: str) -> Optional[float]:
    return None if value is None else _finite(value, field)


def _required(value: Optional[float], field: str) -> float:
    if value is None:
        raise ValueError(f"{field}_required_for_operation")
    return value


def _positive(value: float, field: str) -> float:
    if value <= 0:
        raise ValueError(f"{field}_must_be_positive")
    return value


def _probability_unit(unit: str) -> bool:
    return "probability" in unit.lower() or unit in {"decimal_0_to_1", "share"}


def _currency_unit(unit: str) -> bool:
    return unit == "USD" or unit.startswith("USD_")


def _normalized_float(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if rounded == -0.0 else rounded
