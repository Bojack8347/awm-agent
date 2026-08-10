"""Execution for the lookupRiskReturnFrontier deterministic tool."""

from __future__ import annotations

from typing import Any, Dict

from domain.finance.capital_markets import lookup_risk_return_frontier


def run_lookup_risk_return_frontier(args: Dict[str, Any]) -> Dict[str, Any]:
    """Lookup the deterministic risk-return frontier."""
    try:
        required_return = args.get("required_return_pct")
        target_volatility = args.get("target_volatility_pct")
        full_result = lookup_risk_return_frontier(
            required_return_pct=(
                float(required_return) if required_return is not None else None
            ),
            target_volatility_pct=(
                float(target_volatility) if target_volatility is not None else None
            ),
        )
        return {
            "success": bool(full_result.get("success", False)),
            "full_result": full_result,
            **(
                {"error": full_result.get("error")}
                if not full_result.get("success", False)
                else {}
            ),
        }
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "success": False,
            "error": f"lookupRiskReturnFrontier failed: {exc}",
        }
