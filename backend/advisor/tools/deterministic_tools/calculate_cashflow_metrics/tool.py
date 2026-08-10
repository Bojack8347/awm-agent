"""Agent declaration for stored cash-flow metric calculations."""

TOOL_SPEC = {
    "name": "calculate_cashflow_metrics",
    "capability": "calculation_toolkit",
    "description": (
        "Answer a calculation follow-up from one completed immutable cash-flow "
        "analysis without rerunning LifeModel. Resolve typed metric values by metric "
        "key and optional nested value path, then compute a difference, absolute "
        "difference, ratio, percentage change, probability complement, future value, "
        "present value, or CAGR. The server retrieves the analysis and copies the "
        "source values; never transcribe model numbers into a generic calculator. "
        "For directional binary operations, primary is the base/subtrahend and "
        "secondary is the comparison/minuend: difference is secondary - primary, "
        "ratio is secondary / primary, and percentage_change measures primary to "
        "secondary. For example, 'p90 minus p50' means primary=p50 and secondary=p90. "
        "If a ratio or percentage comparison crosses zero, the result explicitly "
        "marks it as non-intuitive and supplies the signed difference to lead with."
    ),
    "writeback_target": "none",
    "read_only": True,
}


_METRIC_REF = {
    "type": "object",
    "properties": {
        "metric_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": (
                "Canonical typed evidence key. Common cash-flow keys include "
                "terminal_value_percentiles (use value_path p10, p50, or p90), "
                "shortfall_percentiles (p10, p50, or p90), and "
                "success_probability (use null value_path)."
            ),
        },
        "value_path": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 240},
                {"type": "null"},
            ],
            "description": (
                "Path inside the claim value. For terminal_value_percentiles, use "
                "exactly p10, p50, or p90 rather than prefixing percentiles."
            ),
        },
    },
    "required": ["metric_key", "value_path"],
    "additionalProperties": False,
}


PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 160},
                {"type": "null"},
            ]
        },
        "operation": {
            "type": "string",
            "enum": [
                "difference",
                "absolute_difference",
                "ratio",
                "percentage_change",
                "probability_complement",
                "future_value",
                "present_value",
                "compound_annual_growth_rate",
            ],
            "description": (
                "Use percentage_change for 'percent higher/lower', 'percent increase/"
                "decrease', or 'percentage change'. Use ratio only for 'how many times' "
                "or 'what multiple'. A ratio or percentage across opposite signs is "
                "mathematically reported with a warning and signed-difference context."
            ),
        },
        "primary": {
            **_METRIC_REF,
            "description": (
                "Base/subtrahend metric. For 'A minus B', put B here. "
                "For percentage change, this is the starting value."
            ),
        },
        "secondary": {
            "anyOf": [
                _METRIC_REF,
                {"type": "null"},
            ],
            "description": (
                "Comparison/minuend metric. For 'A minus B', put A here. "
                "For percentage change, this is the ending value."
            ),
        },
        "annual_rate_decimal": {
            "anyOf": [
                {"type": "number", "exclusiveMinimum": -1, "maximum": 10},
                {"type": "null"},
            ]
        },
        "periods": {
            "anyOf": [
                {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
                {"type": "null"},
            ]
        },
        "compounds_per_year": {
            "anyOf": [
                {"type": "integer", "minimum": 1, "maximum": 365},
                {"type": "null"},
            ]
        },
    },
    "required": [
        "analysis_id",
        "operation",
        "primary",
        "secondary",
        "annual_rate_decimal",
        "periods",
        "compounds_per_year",
    ],
    "additionalProperties": False,
}
