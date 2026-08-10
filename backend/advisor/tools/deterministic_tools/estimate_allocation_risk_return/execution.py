"""Execution for the estimateAllocationRiskReturn deterministic tool."""

from __future__ import annotations

from typing import Any, Dict

from domain.finance.capital_markets import (
    estimate_account_pool_assumptions,
    estimate_allocation_risk_return,
)


def _as_decimal(value: Any, default: float) -> float:
    """Interpret decimal or percent-like inputs as decimals."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number / 100.0 if number > 1.0 else number


def run_estimate_allocation_risk_return(args: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate expected return and volatility for an allocation or account pool."""
    try:
        allocation = args.get("allocation")
        accounts = args.get("accounts")
        default_return = _as_decimal(args.get("default_return"), 0.0)
        default_volatility = _as_decimal(args.get("default_volatility"), 0.0)

        if isinstance(accounts, list):
            estimate = estimate_account_pool_assumptions(
                accounts,
                default_return=default_return,
                default_volatility=default_volatility,
            )
            input_kind = "account_pool"
        elif isinstance(allocation, dict):
            estimate = estimate_allocation_risk_return(
                allocation,
                default_return=default_return,
                default_volatility=default_volatility,
            )
            input_kind = "allocation"
        else:
            return {
                "success": False,
                "error": "allocation object or accounts array is required",
            }

        full_result = {
            **estimate,
            "input_kind": input_kind,
            "expected_return_pct": round(float(estimate["expected_return"]) * 100.0, 4),
            "volatility_pct": round(float(estimate["volatility"]) * 100.0, 4),
        }
        return {"success": True, "full_result": full_result}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "success": False,
            "error": f"estimateAllocationRiskReturn failed: {exc}",
        }
