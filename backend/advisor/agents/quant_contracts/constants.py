from __future__ import annotations


QUANT_TOOL_NAMES = frozenset(
    {
        "run_cashflow_projection",
        "solve_cashflow_contribution",
        "get_cashflow_analysis",
        "audit_cashflow_analysis",
        "calculate_cashflow_metrics",
        "calculate_financial_math",
        "query_wolfram_alpha",
        "research_public_financial_fact",
        "run_asset_allocation",
        "get_asset_allocation_analysis",
        "compare_quant_analyses",
        "analyze_portfolio_risk",
        "analyze_asset_location",
        "estimateAllocationRiskReturn",
        "lookupRiskReturnFrontier",
    }
)


STRICT_QUANT_TOOL_NAMES = frozenset(
    {
        "run_cashflow_projection",
        "solve_cashflow_contribution",
        "calculate_cashflow_metrics",
        "query_wolfram_alpha",
        "research_public_financial_fact",
        "review_public_fact_reuse",
        "run_asset_allocation",
    }
)


REQUIRED_ALLOCATION_CONSTRAINT_CHECKS = frozenset(
    {
        "engine_response_schema",
        "engine_constraint_contract",
        "signed_assessment",
        "assessment_eligibility",
        "assessment_integrity",
        "mandate_support",
        "hard_exclusions",
        "active_risk",
        "asset_weight_sum",
        "asset_weight_bounds",
        "security_weight_sum",
        "security_weight_bounds",
        "security_dollar_sum",
        "security_notional_reconciliation",
        "asset_class_reconciliation",
        "canonical_asset_classes",
        "portfolio_metrics_finite",
        "target_volatility",
    }
)
