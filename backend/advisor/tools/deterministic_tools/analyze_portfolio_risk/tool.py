"""Agent declaration for portfolio risk contribution and stress analysis."""

TOOL_SPEC = {
    "name": "analyze_portfolio_risk",
    "capability": "portfolio_analytics",
    "description": (
        "Analyze covariance-based component risk contributions, deterministic "
        "one-period stress scenarios, an optional seeded path-based maximum-drawdown "
        "distribution, and optional fee drag for a completed immutable allocation. "
        "Drawdown requires an explicit horizon/path/seed configuration. Fee drag "
        "requires an explicit blended annual fee assumption and does not look up "
        "product expense ratios. This is reporting-only and never recommends trades."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "allocation_analysis_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
        },
        "stress_scenarios": {
            "anyOf": [
                {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                            },
                            "asset_class_shocks": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "number",
                                    "minimum": -1,
                                    "maximum": 1,
                                },
                            },
                        },
                        "required": ["name", "asset_class_shocks"],
                        "additionalProperties": False,
                    },
                },
                {"type": "null"},
            ],
        },
        "drawdown_config": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "horizon_years": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 60,
                        },
                        "num_simulations": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 20000,
                        },
                        "seed": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 2147483647,
                        },
                    },
                    "required": ["horizon_years", "num_simulations", "seed"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "description": (
                "Explicit synthetic monthly path configuration, or null when no "
                "maximum-drawdown distribution is requested."
            ),
        },
        "fee_drag_config": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "annual_fee_bps": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1000,
                        },
                        "horizon_years": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 60,
                        },
                    },
                    "required": ["annual_fee_bps", "horizon_years"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "description": (
                "Explicit blended annual fee scenario, or null. Never infer a "
                "security-level expense ratio."
            ),
        },
    },
    "required": [
        "allocation_analysis_id",
        "stress_scenarios",
        "drawdown_config",
        "fee_drag_config",
    ],
    "additionalProperties": False,
}
