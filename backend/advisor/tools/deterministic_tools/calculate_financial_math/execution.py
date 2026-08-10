"""Pure implementations of bounded financial arithmetic."""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional


_OPERATIONS = {
    "aggregation",
    "difference",
    "percentage_change",
    "percentage_of_base",
    "annual_to_monthly",
    "monthly_to_annual",
    "future_value_lump_sum",
    "present_value_lump_sum",
    "future_value_recurring_contribution",
    "loan_payment",
    "compound_annual_growth_rate",
}


def calculate_financial_math(
    arguments: Dict[str, Any], *, client_id: str = "", companion_turn_id: str = "",
    client_file: Optional[Dict[str, Any]] = None,
    calculation_result_reader: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Return exact formula inputs and a finite deterministic result."""

    if arguments.get("schema_version") == "awm.financial_math.v2":
        from .v2 import evaluate_plan

        return evaluate_plan(
            arguments,
            client_id=client_id,
            companion_turn_id=companion_turn_id,
            client_file=client_file or {},
            calculation_result_reader=calculation_result_reader,
        )

    operation = str(arguments.get("operation") or "").strip()
    if operation not in _OPERATIONS:
        return {"success": False, "error": "financial_math_operation_invalid"}
    if operation == "aggregation":
        try:
            return _aggregation_result(arguments)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
    try:
        primary = _finite_number(arguments.get("primary_value"), "primary_value")
        secondary = _optional_finite_number(arguments.get("secondary_value"), "secondary_value")
        annual_rate = _optional_finite_number(
            arguments.get("annual_rate_decimal"),
            "annual_rate_decimal",
        )
        periods = _optional_finite_number(arguments.get("periods"), "periods")
        payments_per_year = _optional_positive_int(
            arguments.get("payments_per_year"),
            "payments_per_year",
        )
        payment_timing = arguments.get("payment_timing")
        input_unit = str(arguments.get("input_unit") or "").strip()

        value, unit, formula = _execute_operation(
            operation=operation,
            primary=primary,
            secondary=secondary,
            annual_rate=annual_rate,
            periods=periods,
            payments_per_year=payments_per_year,
            payment_timing=payment_timing,
            input_unit=input_unit,
        )
        comparison_context = _comparison_context(
            operation=operation,
            primary=primary,
            secondary=secondary,
            input_unit=input_unit,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if not math.isfinite(value):
        return {"success": False, "error": "financial_math_result_not_finite"}

    normalized_value = _normalized_float(value)
    return {
        "success": True,
        "full_result": {
            "schema_version": "awm.financial_math.v1",
            "operation": operation,
            "inputs": {
                "primary_value": primary,
                "secondary_value": secondary,
                "annual_rate_decimal": annual_rate,
                "periods": periods,
                "payments_per_year": payments_per_year,
                "payment_timing": payment_timing,
                "input_unit": input_unit,
            },
            "result": {
                "value": normalized_value,
                "unit": unit,
            },
            "formula": formula,
            "comparison_context": comparison_context,
            "calculation_policy": (
                "Deterministic arithmetic over explicit inputs only. No cash-flow "
                "model was run and no assumption or recommendation was selected."
            ),
        },
    }


def _aggregation_result(arguments: Dict[str, Any]) -> Dict[str, Any]:
    operands = arguments.get("operands")
    if not isinstance(operands, list) or not operands:
        raise ValueError("financial_aggregation_operands_required")
    currencies = {str(item.get("currency") or "") for item in operands if isinstance(item, dict)}
    if len(currencies) != 1 or "" in currencies:
        raise ValueError("financial_aggregation_currency_mismatch")
    if any(not isinstance(item, dict) or item.get("unit") != "money" for item in operands):
        raise ValueError("financial_aggregation_unit_mismatch")

    total = 0.0
    steps = []
    normalized_inputs = []
    for index, item in enumerate(operands):
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("financial_aggregation_operand_name_required")
        value = _finite_number(item.get("value"), f"operands_{index}_value")
        direction = str(item.get("direction") or "")
        if direction not in {"add", "subtract"}:
            raise ValueError("financial_aggregation_direction_invalid")
        signed_value = value if direction == "add" else -value
        before = total
        total += signed_value
        normalized_inputs.append(
            {
                "name": name,
                "value": value,
                "direction": direction,
                "currency": next(iter(currencies)),
                "unit": "money",
            }
        )
        steps.append(
            {
                "index": index + 1,
                "name": name,
                "operator": "+" if direction == "add" else "-",
                "value": value,
                "subtotal_before": _normalized_float(before),
                "subtotal_after": _normalized_float(total),
            }
        )
    currency = next(iter(currencies))
    return {
        "success": True,
        "full_result": {
            "schema_version": "awm.financial_math.v1",
            "operation": "aggregation",
            "inputs": {"operands": normalized_inputs, "currency": currency, "unit": "money"},
            "result": {"value": _normalized_float(total), "unit": currency},
            "formula": " + ".join(
                ("" if item["direction"] == "add" else "-") + item["name"]
                for item in normalized_inputs
            ),
            "steps": steps,
            "calculation_policy": (
                "Deterministic aggregation over explicit same-currency monetary inputs only."
            ),
        },
    }


def _execute_operation(
    *,
    operation: str,
    primary: float,
    secondary: Optional[float],
    annual_rate: Optional[float],
    periods: Optional[float],
    payments_per_year: Optional[int],
    payment_timing: Any,
    input_unit: str,
) -> tuple[float, str, str]:
    if operation == "difference":
        comparison = _required(secondary, "secondary_value")
        return comparison - primary, input_unit, "secondary_value - primary_value"

    if operation == "percentage_change":
        comparison = _required(secondary, "secondary_value")
        if primary == 0:
            raise ValueError("percentage_change_base_must_be_nonzero")
        return (
            (comparison - primary) / abs(primary),
            "decimal_change",
            "(secondary_value - primary_value) / abs(primary_value)",
        )

    if operation == "percentage_of_base":
        fraction_or_base = _required(secondary, "secondary_value")
        if input_unit not in {"USD", "USD_per_month", "USD_per_year"}:
            raise ValueError("percentage_of_base_requires_currency_input")
        if 0 <= fraction_or_base <= 1:
            return (
                primary * fraction_or_base,
                input_unit,
                "primary_value * secondary_value",
            )
        if fraction_or_base <= 0:
            raise ValueError("percentage_of_base_base_must_be_positive")
        return (
            primary / fraction_or_base,
            "decimal_share",
            "primary_value / secondary_value",
        )

    if operation == "annual_to_monthly":
        if input_unit != "USD_per_year":
            raise ValueError("annual_to_monthly_requires_USD_per_year")
        return primary / 12.0, "USD_per_month", "primary_value / 12"

    if operation == "monthly_to_annual":
        if input_unit != "USD_per_month":
            raise ValueError("monthly_to_annual_requires_USD_per_month")
        return primary * 12.0, "USD_per_year", "primary_value * 12"

    if operation in {
        "future_value_lump_sum",
        "present_value_lump_sum",
        "future_value_recurring_contribution",
        "loan_payment",
    }:
        if input_unit not in {"USD", "USD_per_month", "USD_per_year"}:
            raise ValueError(f"{operation}_requires_currency_input")
        rate = _required(annual_rate, "annual_rate_decimal")
        years = _positive(_required(periods, "periods"), "periods")
        frequency = payments_per_year or 1
        if rate <= -1:
            raise ValueError("annual_rate_decimal_must_be_greater_than_negative_one")
        periodic_rate = rate / frequency
        count = years * frequency
        if count <= 0:
            raise ValueError("period_count_must_be_positive")

        if operation == "future_value_lump_sum":
            value = primary * (1.0 + periodic_rate) ** count
            return (
                value,
                "USD",
                "primary_value * (1 + annual_rate_decimal / payments_per_year) "
                "** (periods * payments_per_year)",
            )
        if operation == "present_value_lump_sum":
            denominator = (1.0 + periodic_rate) ** count
            if denominator == 0:
                raise ValueError("present_value_discount_factor_is_zero")
            return (
                primary / denominator,
                "USD",
                "primary_value / (1 + annual_rate_decimal / payments_per_year) "
                "** (periods * payments_per_year)",
            )
        if operation == "future_value_recurring_contribution":
            timing = _payment_timing(payment_timing)
            if periodic_rate == 0:
                value = primary * count
            else:
                value = primary * (((1.0 + periodic_rate) ** count - 1.0) / periodic_rate)
            if timing == "begin":
                value *= 1.0 + periodic_rate
            return (
                value,
                "USD",
                "payment * annuity_accumulation_factor; multiplied by "
                "(1 + periodic_rate) for beginning-of-period payments",
            )
        if primary < 0:
            raise ValueError("loan_principal_must_be_nonnegative")
        if periodic_rate == 0:
            value = primary / count
        else:
            denominator = 1.0 - (1.0 + periodic_rate) ** (-count)
            if denominator == 0:
                raise ValueError("loan_payment_denominator_is_zero")
            value = primary * periodic_rate / denominator
        return value, _payment_unit(frequency), "principal * periodic_rate / (1 - (1 + periodic_rate) ** -period_count)"

    if operation == "compound_annual_growth_rate":
        ending = _required(secondary, "secondary_value")
        years = _positive(_required(periods, "periods"), "periods")
        if primary <= 0 or ending < 0:
            raise ValueError("cagr_requires_positive_start_and_nonnegative_end")
        return (
            (ending / primary) ** (1.0 / years) - 1.0,
            "annual_decimal",
            "(secondary_value / primary_value) ** (1 / periods) - 1",
        )

    raise ValueError("financial_math_operation_invalid")


def _comparison_context(
    *,
    operation: str,
    primary: float,
    secondary: Optional[float],
    input_unit: str,
) -> Optional[Dict[str, Any]]:
    """Describe sign-crossing percentage changes without changing the arithmetic."""

    if operation != "percentage_change" or secondary is None:
        return None
    signed_difference = _normalized_float(secondary - primary)
    crosses_zero = primary * secondary < 0.0
    return {
        "crosses_zero": crosses_zero,
        "sign_relation": (
            "opposite_sides_of_zero"
            if crosses_zero
            else "same_side_or_zero"
        ),
        "signed_difference": signed_difference,
        "absolute_difference": _normalized_float(abs(signed_difference)),
        "difference_unit": input_unit,
        "lead_metric": (
            "signed_difference" if crosses_zero else "requested_result"
        ),
        "warning": (
            "The source and comparison values are on opposite sides of zero. "
            "The requested percentage change is mathematically defined but is not "
            "an intuitive percent-higher or percent-lower comparison; lead with "
            "the signed difference."
            if crosses_zero
            else None
        ),
    }


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_must_be_numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field}_must_be_finite")
    return number


def _optional_finite_number(value: Any, field: str) -> Optional[float]:
    return None if value is None else _finite_number(value, field)


def _optional_positive_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 365:
        raise ValueError(f"{field}_must_be_an_integer_from_1_to_365")
    return value


def _required(value: Optional[float], field: str) -> float:
    if value is None:
        raise ValueError(f"{field}_required_for_operation")
    return value


def _positive(value: float, field: str) -> float:
    if value <= 0:
        raise ValueError(f"{field}_must_be_positive")
    return value


def _payment_timing(value: Any) -> str:
    timing = str(value or "").strip()
    if timing not in {"end", "begin"}:
        raise ValueError("payment_timing_required_for_recurring_contribution")
    return timing


def _payment_unit(payments_per_year: int) -> str:
    if payments_per_year == 12:
        return "USD_per_month"
    if payments_per_year == 1:
        return "USD_per_year"
    return "USD_per_payment_period"


def _normalized_float(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if rounded == -0.0 else rounded
