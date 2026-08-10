"""Execution logic for portfolio risk analysis."""

from domain.finance.capital_markets import analyze_allocation_risk_contributions


def run_portfolio_risk_analysis(
    *,
    allocation,
    stress_scenarios=None,
    drawdown_config=None,
    fee_drag_config=None,
    initial_investment=None,
):
    try:
        full_result = analyze_allocation_risk_contributions(
            allocation,
            stress_scenarios=stress_scenarios,
            drawdown_config=drawdown_config,
            fee_drag_config=fee_drag_config,
            initial_investment=initial_investment,
        )
        return {"success": True, "full_result": full_result}
    except Exception as exc:
        return {"success": False, "error": f"portfolio_risk_analysis_failed:{exc}"}
