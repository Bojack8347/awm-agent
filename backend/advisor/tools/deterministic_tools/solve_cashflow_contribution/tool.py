"""Agent declaration for deterministic cash-flow contribution solving."""

TOOL_SPEC = {
    "name": "solve_cashflow_contribution",
    "capability": "calculation_toolkit",
    "description": (
        "Solve a bounded recurring monthly taxable-investment contribution against "
        "validated cash-flow model outputs. Use maximum_sustainable to locate the "
        "highest tested contribution satisfying explicit success, shortfall, and "
        "liquidity constraints, or minimum_for_terminal_goal to locate the smallest "
        "tested contribution satisfying those constraints and a median terminal-net-"
        "worth target. The tool reports a bound rather than calling an arbitrary "
        "search ceiling the maximum. Its result identifies the tested feasible "
        "interval, the adjacent transition interval when found, and the constraint "
        "that failed at the binding tested point. It is reporting-only, not a "
        "recommendation."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "objective": {
            "type": "string",
            "enum": ["maximum_sustainable", "minimum_for_terminal_goal"],
        },
        "target_terminal_value": {
            "anyOf": [
                {"type": "number", "minimum": 0},
                {"type": "null"},
            ],
        },
        "minimum_success_probability": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "minimum_p10_liquidity": {"type": "number"},
        "maximum_monthly_contribution": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 1000000,
        },
        "monthly_tolerance": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 10000,
        },
        "start_horizon_years": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
        },
        "duration_years": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
        },
        "monte_carlo_paths": {
            "anyOf": [
                {"type": "integer", "minimum": 10, "maximum": 1000},
                {"type": "null"},
            ],
            "description": (
                "Null evaluates deterministic candidates; an integer evaluates every "
                "candidate with exactly that many Monte Carlo paths."
            ),
        },
        "allocation_analysis_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 160},
                {"type": "null"},
            ],
        },
    },
    "required": [
        "question",
        "objective",
        "target_terminal_value",
        "minimum_success_probability",
        "minimum_p10_liquidity",
        "maximum_monthly_contribution",
        "monthly_tolerance",
        "start_horizon_years",
        "duration_years",
        "monte_carlo_paths",
        "allocation_analysis_id",
    ],
    "additionalProperties": False,
}
