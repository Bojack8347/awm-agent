"""Declared and executable financial-calculation capabilities."""

from __future__ import annotations

from typing import Any, Dict


SUPPORTED_CALCULATION_MANIFEST: Dict[str, Dict[str, Any]] = {
    "net_worth": {
        "operation": "aggregation",
        "operands": [
            {"name": "assets", "value": 100, "direction": "add", "currency": "USD", "unit": "money"},
            {"name": "liabilities", "value": 40, "direction": "subtract", "currency": "USD", "unit": "money"},
        ],
    },
    "sum_account_balances": {
        "operation": "aggregation",
        "operands": [
            {"name": "account_a", "value": 10, "direction": "add", "currency": "USD", "unit": "money"},
            {"name": "account_b", "value": 20, "direction": "add", "currency": "USD", "unit": "money"},
        ],
    },
    "income_minus_spending": {
        "operation": "difference",
        "primary_value": 60,
        "secondary_value": 100,
        "input_unit": "USD_per_year",
    },
    "percentage_of_known_base": {
        "operation": "percentage_of_base",
        "primary_value": 100,
        "secondary_value": 0.10,
        "input_unit": "USD",
    },
    "portion_of_known_base": {
        "operation": "percentage_of_base",
        "primary_value": 30,
        "secondary_value": 100,
        "input_unit": "USD",
    },
    "period_conversion": {
        "operation": "annual_to_monthly",
        "primary_value": 120,
        "input_unit": "USD_per_year",
    },
    "future_value": {
        "operation": "future_value_lump_sum",
        "primary_value": 100,
        "annual_rate_decimal": 0.05,
        "periods": 1,
        "payments_per_year": 1,
        "input_unit": "USD",
    },
    "present_value": {
        "operation": "present_value_lump_sum",
        "primary_value": 105,
        "annual_rate_decimal": 0.05,
        "periods": 1,
        "payments_per_year": 1,
        "input_unit": "USD",
    },
}


def executable_arguments(case: Dict[str, Any]) -> Dict[str, Any]:
    """Fill the nullable common envelope used by the agent tool contract."""

    return {
        "primary_value": 0,
        "secondary_value": None,
        "annual_rate_decimal": None,
        "periods": None,
        "payments_per_year": None,
        "payment_timing": None,
        "input_unit": "USD",
        **case,
    }
